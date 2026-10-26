"""CLI shortsgen.

Perintah:
  init       buat direktori data/log + database
  doctor     validasi konfigurasi + pemeriksaan provider (laporan JSON)
  probe      matriks kapabilitas 9Router (live; --deep untuk uji berbiaya minimal)
  create     buat job baru dari topik (anti-duplikat)
  run        jalankan pipeline (resume otomatis)
  approve    setujui gerbang approval pada mode live
  status     tampilkan status job + stage
  list       daftar job
  demo       contoh cepat satu perintah (mock secara default)
  voice-sample  sampel semua emosi TTS untuk pemeriksaan manusia
  storyboard    tampilkan lokasi contact sheet + spec storyboard
  evaluate      buat/tampilkan laporan evaluasi job
  compare       bandingkan job lama vs baru (metrik + evaluasi)

Kode keluar: 0 sukses, 1 error umum, 2 error konfigurasi,
3 job gagal, 4 menunggu persetujuan.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .config import Config, load_config
from .errors import ConfigError, DedupeConflict, NotFoundError, ShortsGenError
from .logging_setup import get_logger, setup_logging
from .media.ffmpeg import FFmpeg
from .media.voice import choose_voice_profile, write_voice_sample
from .models import STAGE_LABELS_ID, STAGE_ORDER, JobStatus, StageStatus
from .pipeline.orchestrator import Pipeline
from .providers import build_providers
from .qa.evaluation import evaluate_job
from .repository import JobRepository
from .story.narrative import classify_topic
from .util.storage import check_disk_quota, dir_size_bytes, free_disk_bytes, human_mb

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_JOB_FAILED = 3
EXIT_BLOCKED = 4


# ---------------------------------------------------------------------------
# Helper umum
# ---------------------------------------------------------------------------

def _load_config(args: argparse.Namespace) -> Config:
    env_file = Path(args.env_file) if getattr(args, "env_file", None) else None
    config = load_config(env_file)
    overrides: dict[str, Any] = {}
    if getattr(args, "mode", None):
        overrides["mode"] = args.mode
    if getattr(args, "preset", None):
        overrides["render_preset"] = args.preset
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return config


def _open_repo(config: Config) -> JobRepository:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    return JobRepository(config.db_path)


def _ffmpeg_status(config: Config) -> dict[str, Any]:
    from .media.ffmpeg import find_ffmpeg

    ffmpeg = find_ffmpeg(config.ffmpeg_path) or ""
    ffprobe = ""
    if ffmpeg:
        probe = Path(ffmpeg).with_name("ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe")
        ffprobe = str(probe) if probe.exists() else ""
    return {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "available": bool(ffmpeg),
        "needed_for": "render (Fase D)",
    }


# ---------------------------------------------------------------------------
# Perintah
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    config = _load_config(args)
    setup_logging(config.logs_dir)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "reports").mkdir(parents=True, exist_ok=True)
    repo = _open_repo(config)
    repo.close()
    print("Inisialisasi selesai:")
    print(f"  data dir  : {config.data_dir.resolve()}")
    print(f"  logs dir  : {config.logs_dir.resolve()}")
    print(f"  database  : {config.db_path.resolve()}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _load_config(args)
    logger = setup_logging(config.logs_dir)
    report: dict[str, Any] = {
        "tool": "shortsgen doctor",
        "version": __version__,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "config": config.summary(),
        "checks": [],
        "providers": [],
        "warnings": [],
        "errors": [],
    }

    def check(name: str, ok: bool, detail: str, *, fatal: bool = False) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            (report["errors"] if fatal else report["warnings"]).append(f"{name}: {detail}")

    check("python", sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", fatal=True)
    check("data_dir_writable", _is_writable(config.data_dir), str(config.data_dir.resolve()), fatal=True)
    check("logs_dir_writable", _is_writable(config.logs_dir), str(config.logs_dir.resolve()))

    ffmpeg = _ffmpeg_status(config)
    check(
        "ffmpeg",
        ffmpeg["available"],
        f"ffmpeg={ffmpeg['ffmpeg'] or '<tidak ditemukan>'} (dibutuhkan {ffmpeg['needed_for']})",
    )
    report["ffmpeg"] = ffmpeg

    repo = _open_repo(config)
    check("database", True, f"{config.db_path.resolve()} ({repo.job_count()} job tersimpan)")
    repo.close()

    free = free_disk_bytes(config.data_dir)
    used = dir_size_bytes(config.data_dir)
    check("disk_free", free > 500 * 1024 * 1024, f"tersisa {human_mb(max(free, 0))}")
    report["disk"] = {
        "data_used": human_mb(used),
        "free": human_mb(max(free, 0)),
        "quota_mb": config.disk_quota_mb,
    }
    try:
        check_disk_quota(config.data_dir, config.disk_quota_mb)
        check("disk_quota", True, f"pemakaian {human_mb(used)} / kuota {config.disk_quota_mb} MB")
    except ShortsGenError as exc:
        check("disk_quota", False, str(exc))

    # Health provider (mock: offline; live: termasuk versi 9Router + jumlah model)
    if config.is_mock:
        providers = build_providers(config, logger=logger)
        report["providers"] = [h.to_dict() for h in providers.health()]
    else:
        try:
            providers = build_providers(config, logger=logger)
            client = providers.llm.client if providers.llm is not None else None  # type: ignore[attr-defined]
            if client is not None:
                try:
                    health = client.health()
                    check("nine_router_health", bool(health.get("ok")), f"/api/health -> {health}")
                except ShortsGenError as exc:
                    check("nine_router_health", False, str(exc), fatal=True)
                try:
                    models = client.list_models()
                    check("nine_router_models", len(models) > 0, f"{len(models)} model tersedia")
                except ShortsGenError as exc:
                    check("nine_router_models", False, str(exc))
            report["providers"] = [h.to_dict() for h in providers.health()]
            if args.deep:
                report["providers"] += [
                    h.to_dict()
                    for h in _deep_probes(providers, include_media=args.deep_media)
                ]
        except ConfigError as exc:
            check("providers", False, str(exc), fatal=True)

    report["ok"] = not report["errors"]

    # Tulis laporan ke data/reports/
    reports_dir = config.data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"doctor-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_doctor(report)
        print(f"\nLaporan lengkap: {report_path}")

    return EXIT_OK if report["ok"] else EXIT_CONFIG


def _deep_probes(providers: Any, *, include_media: bool) -> list[Any]:
    results = []
    llm = providers.llm
    if llm is not None and hasattr(llm, "probe"):
        results.append(llm.probe())
    tts = providers.tts
    if tts is not None and hasattr(tts, "probe"):
        results.append(tts.probe())
    stock = getattr(providers, "stock", None)
    if stock is not None and hasattr(stock, "probe"):
        results.append(stock.probe())  # pencarian kecil gratis
    if include_media and providers.image is not None:
        from .providers.base import ProviderHealth
        from .errors import ProviderError

        try:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                result = providers.image.generate(
                    "lingkaran merah sederhana di latar gelap", Path(tmp) / "probe.bin"
                )
            results.append(
                ProviderHealth(
                    provider=result.provider,
                    capability="image",
                    status="available",
                    detail=f"probe ok: {result.mime}, {result.bytes_written} byte",
                    latency_ms=result.latency_ms,
                )
            )
        except ProviderError as exc:
            results.append(
                ProviderHealth(
                    provider="9router",
                    capability="image",
                    status="error",
                    detail=f"probe gagal: {exc}",
                )
            )
    return results


def _print_doctor(report: dict[str, Any]) -> None:
    print("== shortsgen doctor ==")
    print(f"versi {report['version']} | mode: {report['config']['mode']}")
    print("\nPemeriksaan:")
    for item in report["checks"]:
        print(f"  [{'OK ' if item['ok'] else '!! '}] {item['name']}: {item['detail']}")
    if report["providers"]:
        print("\nProvider / kapabilitas:")
        for item in report["providers"]:
            latency = f" ({item['latency_ms']} ms)" if item.get("latency_ms") else ""
            print(f"  - {item['capability']:<7} {item['status']:<15} {item['provider']}{latency}")
            if item.get("detail"):
                print(f"      {item['detail']}")
    if report["warnings"]:
        print("\nPeringatan:")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    if report["errors"]:
        print("\nError:")
        for error in report["errors"]:
            print(f"  - {error}")


def cmd_probe(args: argparse.Namespace) -> int:
    """Matriks kapabilitas 9Router langsung dari klien (live)."""
    config = _load_config(args)
    logger = setup_logging(config.logs_dir)
    if config.is_mock:
        print("Probe membutuhkan mode live (APP_MODE=live atau --mode live).")
        return EXIT_CONFIG
    providers = build_providers(config, logger=logger)
    llm = providers.llm
    client = llm.client  # type: ignore[attr-defined]
    rows: list[dict[str, Any]] = []
    try:
        health = client.health()
        rows.append({"capability": "health", "status": "ok" if health.get("ok") else "error", "detail": health})
        models = client.list_models()
        ids = [m.get("id") for m in models]
        rows.append({"capability": "models", "status": "ok", "detail": f"{len(models)} model", "models": ids[:5]})
    except ShortsGenError as exc:
        rows.append({"capability": "health/models", "status": "error", "detail": str(exc)})
    rows += [h.to_dict() for h in providers.health()]
    if args.deep:
        rows += [h.to_dict() for h in _deep_probes(providers, include_media=args.deep_media)]
    if args.json:
        print(json.dumps({"base_url": config.nine_router_base_url, "probes": rows}, ensure_ascii=False, indent=2))
    else:
        print(f"9Router: {config.nine_router_base_url}")
        for row in rows:
            print(f"  - {row['capability']:<10} {row['status']:<15} {row.get('detail', '')}")
    return EXIT_OK


def _create_job(args: argparse.Namespace, config: Config) -> tuple[JobRepository, str]:
    repo = _open_repo(config)
    job = repo.create_job(
        args.topic.strip(),
        config.mode,
        config_snapshot=config.summary(),
        force=args.force,
    )
    return repo, job.id


def cmd_create(args: argparse.Namespace) -> int:
    config = _load_config(args)
    setup_logging(config.logs_dir)
    try:
        repo, job_id = _create_job(args, config)
    except DedupeConflict as exc:
        print(f"DUPLIKAT: {exc}")
        return EXIT_ERROR
    finally:
        pass
    job = repo.get_job(job_id)
    repo.close()
    print(f"Job dibuat: {job.id}")
    print(f"  topik: {job.topic}")
    print(f"  mode : {job.mode}")
    print(f"Jalankan: python -m shortsgen run --job {job.id}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    config = _load_config(args)
    logger = setup_logging(config.logs_dir)
    repo = _open_repo(config)
    try:
        job = repo.get_job(args.job)
    except NotFoundError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    providers = build_providers(config, logger=logger)
    pipeline = Pipeline(repo, config, providers, logger=logger)
    result = pipeline.run(job_id=job.id, upto=args.upto, retry_failed=not args.no_retry)
    _print_result(result)
    repo.close()
    if result.status == JobStatus.DONE:
        return EXIT_OK
    if result.status == JobStatus.AWAITING_APPROVAL:
        return EXIT_BLOCKED
    return EXIT_JOB_FAILED


def _print_result(result: Any) -> None:
    print(f"Job {result.job_id}: {result.status}")
    if result.stages_done:
        print("  selesai  : " + ", ".join(result.stages_done))
    if result.stages_skipped:
        print("  dilewati : " + ", ".join(result.stages_skipped) + " (sudah selesai sebelumnya)")
    if result.stages_gap:
        print("  belum ada handler: " + ", ".join(result.stages_gap))
    if result.blocked_stage:
        print(f"  menunggu : {result.blocked_stage} (jalankan: shortsgen approve --job {result.job_id})")
    if result.error:
        print(f"  error    : {result.error}")


def cmd_auth(args: argparse.Namespace) -> int:
    """Login OAuth YouTube sekali; token disimpan lokal (tanpa mencetak rahasia)."""
    config = _load_config(args)
    setup_logging(config.logs_dir)
    from .youtube.auth import YouTubeAuthError, load_credentials

    try:
        credentials = load_credentials(
            config.youtube_client_secrets_file,
            config.youtube_token_file,
            interactive=True,
            force=bool(args.force),
        )
    except YouTubeAuthError as exc:
        print(f"ERROR: {exc}")
        return EXIT_ERROR
    print("OAuth YouTube berhasil.")
    print(f"  token : {config.youtube_token_file}")
    print(f"  scope : {', '.join(credentials.scopes or []) or '-'}")
    try:
        from googleapiclient.discovery import build

        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        response = service.channels().list(part="id,snippet", mine=True).execute()
        items = response.get("items") or []
        if not items:
            print(
                "  channel: TIDAK ADA. Akun Google ini belum punya channel YouTube.\n"
                "           Buat channel di https://www.youtube.com/create_channel atau\n"
                "           login ulang dengan akun pemilik channel (auth --force)."
            )
        else:
            for item in items:
                snippet = item.get("snippet") or {}
                print(f"  channel: {snippet.get('title')} ({item.get('id')})")
    except Exception as exc:  # noqa: BLE001 - informasi tambahan, bukan kegagalan auth
        print(f"  channel: gagal diperiksa ({str(exc)[:160]})")
    print("Upload tetap default private; jalankan setelah approval.")
    return EXIT_OK


def cmd_approve(args: argparse.Namespace) -> int:
    config = _load_config(args)
    setup_logging(config.logs_dir)
    repo = _open_repo(config)
    providers = build_providers(config)
    pipeline = Pipeline(repo, config, providers)
    try:
        pipeline.approve(args.job, note=args.note or "", visibility=getattr(args, "visibility", "private"))
    except ShortsGenError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    print(f"Approval untuk {args.job} dicatat. Lanjutkan: python -m shortsgen run --job {args.job}")
    repo.close()
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    repo = _open_repo(config)
    try:
        job = repo.get_job(args.job)
    except NotFoundError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    print(f"Job {job.id} | {job.status}")
    print(f"  topik     : {job.topic}")
    print(f"  mode      : {job.mode}")
    print(f"  dibuat    : {job.created_at}")
    print(f"  diperbarui: {job.updated_at}")
    if job.last_error:
        print(f"  error     : {job.last_error}")
    stages = repo.get_stages(job.id)
    print("  stage:")
    for stage in STAGE_ORDER:
        record = stages.get(stage)
        if record is None:
            print(f"    - {stage:<10} {'pending':<8}")
            continue
        attempts = f" (attempt {record.attempt})" if record.attempt > 1 else ""
        print(f"    - {stage:<10} {record.status:<8}{attempts} {STAGE_LABELS_ID.get(stage, '')}")
        if record.error:
            print(f"        error: {record.error}")
        if record.artifacts:
            keys = ", ".join(sorted(record.artifacts.keys()))
            print(f"        artifact: {keys}")
    usage = repo.usage_summary(job.id)
    if usage["by_provider"]:
        print("  pemakaian:")
        for key, bucket in usage["by_provider"].items():
            units = ", ".join(f"{k}={v:g}" for k, v in bucket["units"].items()) or "n/a"
            print(f"    - {key}: {bucket['calls']} panggilan | {units} | est. biaya ${bucket['cost_estimate']:.4f}")
        print(f"    total estimasi biaya: ${usage['total_cost_estimate']:.4f} (perkiraan, bukan tagihan)")
    print(f"\n  resume berikutnya: {repo.next_pending_stage(job.id) or '<tidak ada (selesai)>'}")
    repo.close()
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    config = _load_config(args)
    repo = _open_repo(config)
    jobs = repo.list_jobs(limit=args.limit)
    if not jobs:
        print("(belum ada job)")
    else:
        print(f"{'job_id':<18} {'status':<16} {'mode':<6} topik")
        for job in jobs:
            print(f"{job.id:<18} {job.status:<16} {job.mode:<6} {job.topic[:60]}")
    repo.close()
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    """Contoh cepat: buat + jalankan job (mock secara default lewat --mode)."""
    if not getattr(args, "mode", None):
        args.mode = "mock"
    config = _load_config(args)
    logger = setup_logging(config.logs_dir)
    repo = _open_repo(config)
    try:
        job = repo.create_job(
            args.topic.strip(), config.mode, config_snapshot=config.summary(), force=args.force
        )
    except DedupeConflict as exc:
        print(f"DUPLIKAT: {exc}")
        repo.close()
        return EXIT_ERROR
    print(f"Job demo: {job.id} (mode {config.mode})")
    providers = build_providers(config, logger=logger)
    pipeline = Pipeline(repo, config, providers, logger=logger)
    result = pipeline.run(job_id=job.id)
    _print_result(result)
    print(f"\nDetail: python -m shortsgen status --job {job.id}")
    repo.close()
    if result.status == JobStatus.DONE:
        return EXIT_OK
    if result.status == JobStatus.AWAITING_APPROVAL:
        return EXIT_BLOCKED
    return EXIT_JOB_FAILED


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def cmd_voice_sample(args: argparse.Namespace) -> int:
    """Buat sampel semua emosi untuk pemeriksaan manusia (bukan klaim kualitas)."""
    config = _load_config(args)
    setup_logging(config.logs_dir)
    providers = build_providers(config)
    if providers.tts is None:
        print("ERROR: provider TTS tidak dikonfigurasi")
        return EXIT_ERROR
    archetype = classify_topic(args.topic)
    profile = choose_voice_profile(
        args.topic,
        archetype,
        requested=config.tts_voice_profile,
        fallback_voice_mode=config.tts_voice_mode,
    )
    voice = args.voice or profile.voice
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = config.data_dir / "voice_samples" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = FFmpeg(config.ffmpeg_path)
    print(f"Profil: {profile.id} ({profile.name}) | suara: {voice}")
    print(f"Engine: {getattr(providers.tts, 'name', 'unknown')}")
    try:
        report = write_voice_sample(
            providers.tts,
            out_dir / "voice_sample.mp3",
            voice=voice,
            ffmpeg=ffmpeg,
        )
    except Exception as exc:
        print(f"ERROR: gagal membuat sampel: {exc}")
        return EXIT_ERROR
    report.update(
        {
            "profile": profile.id,
            "profile_name": profile.name,
            "topic": args.topic,
            "archetype": archetype,
            "engine": getattr(providers.tts, "name", "unknown"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    report_path = out_dir / "voice_sample.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Sampel : {report['path']}")
    print(f"Laporan: {report_path}")
    print("Catatan: emosi dibuat dari prosody TTS, bukan akting manusia.")
    return EXIT_OK


def cmd_storyboard(args: argparse.Namespace) -> int:
    config = _load_config(args)
    repo = _open_repo(config)
    try:
        job = repo.get_job(args.job)
    except NotFoundError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    artifacts = config.jobs_dir / job.id / "artifacts"
    preview = artifacts / "storyboard_preview.png"
    spec = artifacts / "storyboard.md"
    board = artifacts / "storyboard.json"
    if not board.exists():
        print(f"ERROR: storyboard untuk {job.id} belum ada (jalankan sampai stage scenes)")
        repo.close()
        return EXIT_ERROR
    print(f"Storyboard {job.id}")
    print(f"  spec     : {spec if spec.exists() else '-'}")
    print(f"  data     : {board}")
    print(f"  preview  : {preview if preview.exists() else '-'}")
    try:
        data = json.loads(board.read_text(encoding="utf-8"))
        print(
            f"  ringkas  : {len(data.get('scenes') or [])} scene, "
            f"{data.get('timeline', {}).get('total_seconds')} dtk, "
            f"style {data.get('style_bible', {}).get('id')}"
        )
    except (OSError, json.JSONDecodeError):
        pass
    repo.close()
    return EXIT_OK


def cmd_rerun(args: argparse.Namespace) -> int:
    """Reset stage mulai titik tertentu agar dapat dijalankan ulang."""
    config = _load_config(args)
    setup_logging(config.logs_dir)
    repo = _open_repo(config)
    try:
        job = repo.get_job(args.job)
    except NotFoundError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    try:
        stages = repo.reset_stages(job.id, args.from_stage)
    except NotFoundError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    print(f"Job {job.id}: stage direset dari '{args.from_stage}': {', '.join(stages)}")
    print(f"Jalankan ulang: python -m shortsgen run --job {job.id}")
    repo.close()
    return EXIT_OK


def cmd_evaluate(args: argparse.Namespace) -> int:
    config = _load_config(args)
    setup_logging(config.logs_dir)
    repo = _open_repo(config)
    try:
        job = repo.get_job(args.job)
    except NotFoundError as exc:
        print(f"ERROR: {exc}")
        repo.close()
        return EXIT_ERROR
    artifacts = config.jobs_dir / job.id / "artifacts"
    if not artifacts.exists():
        print(f"ERROR: artefak job {job.id} tidak ditemukan")
        repo.close()
        return EXIT_ERROR
    ctx = types.SimpleNamespace(job=job, repo=repo, artifacts_dir=artifacts)
    try:
        evaluation = evaluate_job(ctx, config)
    except Exception as exc:
        print(f"ERROR: evaluasi gagal: {exc}")
        repo.close()
        return EXIT_ERROR
    print(f"Evaluasi {job.id}: {evaluation.get('status')}")
    print(f"  JSON: {artifacts / 'evaluation.json'}")
    print(f"  MD  : {artifacts / 'evaluation.md'}")
    review = evaluation.get("needs_human_review") or []
    if review:
        print("  perlu tinjauan manusia:")
        for item in review:
            print(f"    - {item.get('code')}: {item.get('message')}")
    repo.close()
    return EXIT_OK


def cmd_compare(args: argparse.Namespace) -> int:
    config = _load_config(args)
    repo = _open_repo(config)
    rows: list[dict[str, Any]] = []
    for job_id in (args.old, args.new):
        try:
            job = repo.get_job(job_id)
        except NotFoundError as exc:
            print(f"ERROR: {exc}")
            repo.close()
            return EXIT_ERROR
        artifacts = config.jobs_dir / job.id / "artifacts"
        ctx = types.SimpleNamespace(job=job, repo=repo, artifacts_dir=artifacts)
        if not (artifacts / "evaluation.json").exists():
            try:
                evaluate_job(ctx, config)
            except Exception:
                pass
        rows.append(_comparison_row(job.id, artifacts))
    old, new = rows
    metrics = [
        ("status", "status"),
        ("durasi (s)", "duration_s"),
        ("shot", "shot_count"),
        ("maks shot (s)", "max_shot_s"),
        ("potongan/menit", "cuts_per_minute"),
        ("relevansi (0-10)", "relevance"),
        ("gaya visual", "style"),
        ("profil suara", "voice_profile"),
        ("prosody", "prosody"),
        ("loudness (LUFS)", "loudness"),
        ("masalah QA", "qa_issues"),
        ("perlu review", "human_review"),
    ]
    lines = [
        f"# Perbandingan job\n\n| metrik | lama ({old['job']}) | baru ({new['job']}) |\n|---|---|---|"
    ]
    for label, key in metrics:
        lines.append(f"| {label} | {old.get(key)} | {new.get(key)} |")
    markdown = "\n".join(lines) + "\n"
    print(markdown)
    out_path = config.jobs_dir / args.new / "artifacts" / "comparison.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"Tersimpan: {out_path}")
    repo.close()
    return EXIT_OK


def _comparison_row(job_id: str, artifacts: Path) -> dict[str, Any]:
    render = _read_json(artifacts / "render.json")
    qa = _read_json(artifacts / "qa.json")
    evaluation = _read_json(artifacts / "evaluation.json")
    visual = evaluation.get("visual") or {}
    audio = evaluation.get("audio") or {}
    editing = evaluation.get("editing") or {}
    shots = render.get("shots") or []
    duration = float(render.get("duration_s") or 0.0)
    return {
        "job": job_id,
        "status": evaluation.get("status") or ("pass" if qa.get("passed") else "fail"),
        "duration_s": round(duration, 2),
        "shot_count": len(shots),
        "max_shot_s": render.get("max_shot_s"),
        "cuts_per_minute": round(len(shots) / max(duration, 1e-6) * 60, 1) if shots else 0,
        "relevance": visual.get("relevance_average"),
        "style": visual.get("style_id"),
        "voice_profile": audio.get("voice_profile"),
        "prosody": audio.get("prosody_supported"),
        "loudness": (audio.get("loudness") or {}).get("input_i"),
        "qa_issues": len(qa.get("issues") or []),
        "human_review": len(evaluation.get("needs_human_review") or []),
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shortsgen",
        description="Generator YouTube Shorts otomatis (9Router + FFmpeg + YouTube Data API v3)",
    )
    parser.add_argument("--version", action="version", version=f"shortsgen {__version__}")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--env-file", help="path file .env alternatif")
        sub.add_argument("--mode", choices=["mock", "live"], help="override APP_MODE")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser("init", help="buat direktori data/log + database")
    add_common(p_init)
    p_init.set_defaults(func=cmd_init)

    p_doctor = subparsers.add_parser("doctor", help="validasi konfigurasi + pemeriksaan provider")
    add_common(p_doctor)
    p_doctor.add_argument("--json", action="store_true", help="cetak laporan JSON")
    p_doctor.add_argument("--deep", action="store_true", help="probe berbiaya minimal (1 chat + 1 TTS kecil)")
    p_doctor.add_argument("--deep-media", action="store_true", help="termasuk 1 uji image (kuota provider terpakai)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_probe = subparsers.add_parser("probe", help="matriks kapabilitas 9Router (live)")
    add_common(p_probe)
    p_probe.add_argument("--json", action="store_true")
    p_probe.add_argument("--deep", action="store_true")
    p_probe.add_argument("--deep-media", action="store_true")
    p_probe.set_defaults(func=cmd_probe)

    p_create = subparsers.add_parser("create", help="buat job baru dari topik")
    add_common(p_create)
    p_create.add_argument("--topic", required=True, help="topik video")
    p_create.add_argument("--force", action="store_true", help="abaikan cek duplikat")
    p_create.set_defaults(func=cmd_create)

    p_run = subparsers.add_parser("run", help="jalankan pipeline (resume otomatis)")
    add_common(p_run)
    p_run.add_argument("--job", required=True, help="job id")
    p_run.add_argument("--upto", choices=list(STAGE_ORDER), help="berhenti setelah stage ini")
    p_run.add_argument("--no-retry", action="store_true", help="jangan ulangi stage yang gagal")
    p_run.add_argument("--preset", choices=["fast", "final"], help="override RENDER_PRESET")
    p_run.set_defaults(func=cmd_run)

    p_auth = subparsers.add_parser("auth", help="login OAuth YouTube (sekali, Desktop client)")
    add_common(p_auth)
    p_auth.add_argument("--force", action="store_true", help="abaikan token lama, login ulang")
    p_auth.set_defaults(func=cmd_auth)

    p_approve = subparsers.add_parser("approve", help="setujui gerbang approval (mode live)")
    add_common(p_approve)
    p_approve.add_argument("--job", required=True)
    p_approve.add_argument("--note", default="")
    p_approve.add_argument("--visibility", choices=["private", "unlisted", "public"], default="private",
                           help="visibilitas upload yang disetujui khusus job ini (default private)")
    p_approve.set_defaults(func=cmd_approve)

    p_status = subparsers.add_parser("status", help="status job + stage")
    add_common(p_status)
    p_status.add_argument("--job", required=True)
    p_status.set_defaults(func=cmd_status)

    p_list = subparsers.add_parser("list", help="daftar job")
    add_common(p_list)
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_demo = subparsers.add_parser("demo", help="contoh cepat (mock default)")
    add_common(p_demo)
    p_demo.add_argument("--topic", required=True)
    p_demo.add_argument("--force", action="store_true")
    p_demo.add_argument("--preset", choices=["fast", "final"], help="override RENDER_PRESET")
    p_demo.set_defaults(func=cmd_demo)

    p_voice = subparsers.add_parser("voice-sample", help="sampel semua emosi TTS")
    add_common(p_voice)
    p_voice.add_argument("--topic", required=True, help="topik untuk memilih profil suara")
    p_voice.add_argument("--voice", help="override id suara edge-tts")
    p_voice.set_defaults(func=cmd_voice_sample)

    p_board = subparsers.add_parser("storyboard", help="lokasi contact sheet + spec storyboard")
    add_common(p_board)
    p_board.add_argument("--job", required=True)
    p_board.set_defaults(func=cmd_storyboard)

    p_eval = subparsers.add_parser("evaluate", help="buat/tampilkan laporan evaluasi job")
    add_common(p_eval)
    p_eval.add_argument("--job", required=True)
    p_eval.set_defaults(func=cmd_evaluate)

    p_compare = subparsers.add_parser("compare", help="bandingkan job lama vs baru")
    add_common(p_compare)
    p_compare.add_argument("--old", required=True, help="job id lama")
    p_compare.add_argument("--new", required=True, help="job id baru")
    p_compare.set_defaults(func=cmd_compare)

    p_rerun = subparsers.add_parser("rerun", help="reset stage mulai titik tertentu")
    add_common(p_rerun)
    p_rerun.add_argument("--job", required=True)
    p_rerun.add_argument("--from-stage", required=True, choices=list(STAGE_ORDER))
    p_rerun.set_defaults(func=cmd_rerun)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print("KONFIGURASI TIDAK VALID:")
        for issue in exc.issues:
            print(f"  - {issue}")
        print("\nLihat .env.example untuk daftar variabel yang benar.")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\nDibatalkan oleh pengguna.")
        return EXIT_ERROR
    except ShortsGenError as exc:
        print(f"ERROR: {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
