"""Orkestrator pipeline dengan checkpoint, resume, dan gerbang persetujuan.

Desain:
- Setiap stage punya `StageHandler` (Protocol). Fase B menyediakan handler
  placeholder yang didaftarkan per stage; fase berikutnya menggantinya satu per
  satu tanpa mengubah orkestrator (dependency injection).
- State disimpan di SQLite (JobRepository) setelah SETIAP stage, sehingga
  kegagalan provider tidak mengharuskan mengulang dari awal (`resume`).
- Stage `approval` adalah gerbang manusia: pada mode live ia menghentikan
  pipeline dengan status BLOCKED sampai pengguna menyetujui (`shortsgen approve`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from ..config import Config
from ..errors import ShortsGenError, StageBlocked
from ..logging_setup import get_logger, get_redactor
from ..models import (
    STAGE_LABELS_ID,
    STAGE_ORDER,
    Job,
    JobStatus,
    StageStatus,
    utcnow_iso,
)
from ..providers.base import ProviderBundle
from ..repository import JobRepository
from ..util.storage import check_disk_quota


@dataclass
class StageContext:
    """Konteks yang diberikan ke setiap handler stage."""

    job: Job
    config: Config
    providers: ProviderBundle
    repo: JobRepository
    logger: Any
    artifacts_dir: Path

    def artifact_path(self, name: str) -> Path:
        path = self.artifacts_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class StageHandler(Protocol):
    stage: str

    def run(self, ctx: StageContext) -> dict[str, Any]:
        """Jalankan stage; kembalikan dict artifact (path/metadata) untuk checkpoint."""
        ...


@dataclass
class PipelineResult:
    job_id: str
    status: str
    stages_done: list[str] = field(default_factory=list)
    stages_skipped: list[str] = field(default_factory=list)
    stages_gap: list[str] = field(default_factory=list)
    failed_stage: Optional[str] = None
    blocked_stage: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "stages_done": self.stages_done,
            "stages_skipped": self.stages_skipped,
            "stages_gap": self.stages_gap,
            "failed_stage": self.failed_stage,
            "blocked_stage": self.blocked_stage,
            "error": self.error,
        }


class Pipeline:
    """Menjalankan stage berurutan dengan dukungan resume."""

    def __init__(
        self,
        repo: JobRepository,
        config: Config,
        providers: ProviderBundle,
        handlers: Optional[dict[str, StageHandler]] = None,
        *,
        logger: Optional[Any] = None,
    ):
        self.repo = repo
        self.config = config
        self.providers = providers
        self.logger = logger or get_logger("pipeline")
        if handlers is None:
            # Registry stage yang sudah diimplementasikan (Fase C dan seterusnya).
            from .stages import build_handlers

            handlers = build_handlers(config, providers, logger=self.logger)
        self.handlers = handlers
        self.redactor = get_redactor()

    # -- publik -------------------------------------------------------

    def run(
        self,
        job_id: str,
        *,
        upto: Optional[str] = None,
        retry_failed: bool = True,
    ) -> PipelineResult:
        """Jalankan stage yang belum selesai. Aman dipanggil ulang (resume)."""
        try:
            return self._run_stages(job_id, upto=upto, retry_failed=retry_failed)
        finally:
            self._write_cache_report(job_id)

    def _write_cache_report(self, job_id: str) -> None:
        """Tulis artifacts/cache.json (hit/miss kumulatif) — gagal tulis tidak fatal."""
        import json

        try:
            stats = self.providers.cache_stats()
        except Exception:  # noqa: BLE001 - pelaporan tidak boleh menggagalkan pipeline
            return
        path = self.config.jobs_dir / job_id / "artifacts" / "cache.json"
        previous: dict[str, Any] = {}
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
        merged: dict[str, dict[str, Any]] = {}
        for capability, current in stats.items():
            prior = (previous.get("caches") or {}).get(capability) or {}
            entry = dict(current)
            for key in ("hits", "misses", "writes"):
                entry[key] = int(prior.get(key) or 0) + int(current.get(key) or 0)
            merged[capability] = entry
        payload = {
            "job_id": job_id,
            "runs": int(previous.get("runs") or 0) + 1,
            "updated_at": utcnow_iso(),
            "caches": merged or (previous.get("caches") or {}),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return

    def _run_stages(
        self,
        job_id: str,
        *,
        upto: Optional[str] = None,
        retry_failed: bool = True,
    ) -> PipelineResult:
        job = self.repo.get_job(job_id)
        if upto is not None and upto not in STAGE_ORDER:
            raise ValueError(f"Stage batas tidak dikenal: {upto}")
        limit_index = STAGE_ORDER.index(upto) if upto else len(STAGE_ORDER) - 1

        result = PipelineResult(job_id=job_id, status=JobStatus.RUNNING)
        self.repo.set_job_status(job_id, JobStatus.RUNNING)

        for stage in STAGE_ORDER:
            if STAGE_ORDER.index(stage) > limit_index:
                break
            record = self.repo.get_stage(job_id, stage)

            if record and record.status in (StageStatus.DONE, StageStatus.SKIPPED):
                result.stages_skipped.append(stage)
                continue

            if record and record.status == StageStatus.BLOCKED and stage == "approval":
                result.status = JobStatus.AWAITING_APPROVAL
                result.blocked_stage = stage
                self.repo.set_job_status(job_id, JobStatus.AWAITING_APPROVAL)
                return result

            if record and record.status == StageStatus.FAILED and not retry_failed:
                result.status = JobStatus.FAILED
                result.failed_stage = stage
                result.error = record.error
                return result

            handler = self.handlers.get(stage)
            if handler is None:
                self.repo.start_stage(job_id, stage)
                self.repo.finish_stage(
                    job_id,
                    stage,
                    StageStatus.SKIPPED,
                    artifacts={"note": f"handler '{stage}' belum diimplementasikan (fase berikutnya)"},
                )
                result.stages_gap.append(stage)
                continue

            # Kuota disk diperiksa sebelum tiap stage.
            try:
                check_disk_quota(self.config.data_dir, self.config.disk_quota_mb)
            except ShortsGenError as exc:
                safe_error = self.redactor.redact(str(exc))
                self.repo.set_job_status(job_id, JobStatus.FAILED, error=safe_error)
                result.status = JobStatus.FAILED
                result.failed_stage = stage
                result.error = safe_error
                self.logger.error(f"[{job_id}] kuota disk terlampaui sebelum stage '{stage}'")
                return result

            ctx = StageContext(
                job=self.repo.get_job(job_id),
                config=self.config,
                providers=self.providers,
                repo=self.repo,
                logger=self.logger,
                artifacts_dir=self.config.jobs_dir / job_id / "artifacts",
            )
            self.repo.start_stage(job_id, stage)
            self.logger.info(f"[{job_id}] stage '{stage}' mulai: {STAGE_LABELS_ID.get(stage, stage)}")
            try:
                artifacts = handler.run(ctx) or {}
            except StageBlocked as blocked:
                self.repo.finish_stage(job_id, stage, StageStatus.BLOCKED, error=str(blocked))
                self.repo.set_job_status(job_id, JobStatus.AWAITING_APPROVAL)
                result.status = JobStatus.AWAITING_APPROVAL
                result.blocked_stage = stage
                return result
            except Exception as exc:  # noqa: BLE001 - semua kegagalan stage dicatat
                safe_error = self.redactor.redact(f"{exc.__class__.__name__}: {exc}")
                self.repo.finish_stage(job_id, stage, StageStatus.FAILED, error=safe_error)
                self.repo.set_job_status(job_id, JobStatus.FAILED, error=safe_error)
                self.logger.error(f"[{job_id}] stage '{stage}' gagal: {safe_error}")
                result.status = JobStatus.FAILED
                result.failed_stage = stage
                result.error = safe_error
                return result

            self.repo.finish_stage(job_id, stage, StageStatus.DONE, artifacts=artifacts)
            result.stages_done.append(stage)
            self.logger.info(f"[{job_id}] stage '{stage}' selesai")

        self.repo.set_job_status(job_id, JobStatus.DONE)
        result.status = JobStatus.DONE
        return result

    def approve(
        self,
        job_id: str,
        *,
        note: str = "",
        visibility: str = "private",
        publish_at: Optional[str] = None,
    ) -> None:
        """Setujui stage `approval` yang tertahan (gerbang manusia).

        `publish_at` (ISO-8601) opsional; bila diisi, stage `schedule` akan
        menjadwalkan penayangan dengan `publishAt` tersebut.
        """
        if visibility not in ("private", "unlisted", "public"):
            raise ValueError("Visibilitas approval tidak valid")
        if publish_at is not None:
            from .stages.schedule import parse_publish_at, validate_publish_at

            validate_publish_at(parse_publish_at(publish_at))
        record = self.repo.get_stage(job_id, "approval")
        if record is None or record.status != StageStatus.BLOCKED:
            raise StageBlocked("approval", "Tidak ada stage approval yang menunggu persetujuan")
        artifacts = {"approved": True, "approved_at": utcnow_iso(), "note": note,
                     "upload_visibility": visibility}
        if publish_at is not None:
            artifacts["publish_at"] = publish_at
        self.repo.finish_stage(
            job_id,
            "approval",
            StageStatus.DONE,
            artifacts=artifacts,
        )
        self.repo.set_job_status(job_id, JobStatus.RUNNING)
        self.repo.add_event(job_id, "info", "Stage approval disetujui oleh pengguna", {"note": note})


# ---------------------------------------------------------------------------
# Handler placeholder Fase B
# ---------------------------------------------------------------------------

def _placeholder_artifact(ctx: StageContext, stage: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    import json

    payload = {
        "stage": stage,
        "placeholder": True,
        "phase": "B",
        "created_at": utcnow_iso(),
        "mode": ctx.config.mode,
        "note": "Handler Fase B; implementasi nyata menyusul pada fase berikutnya.",
    }
    if extra:
        payload.update(extra)
    path = ctx.artifact_path(f"{stage}.json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"artifact": str(path), "placeholder": True}


class ApprovalGate:
    """Mode mock: lolos otomatis (tercatat). Mode live: menunggu persetujuan manusia."""

    stage = "approval"

    def run(self, ctx: StageContext) -> dict[str, Any]:
        if ctx.config.is_mock:
            return _placeholder_artifact(
                ctx, "approval", {"auto_approved": True, "reason": "mode mock"}
            )
        raise StageBlocked(
            "approval",
            "Menunggu persetujuan pengguna. Jalankan: shortsgen approve --job "
            f"{ctx.job.id} setelah memeriksa pratinjau.",
        )


def default_handlers() -> dict[str, StageHandler]:
    """Handler yang tidak tergantung provider: gerbang approval.

    Handler stage lain dirakit otomatis oleh `Pipeline` dari registry
    (`shortsgen.pipeline.stages.build_handlers`) sehingga selalu sinkron
    dengan stage yang benar-benar sudah diimplementasikan.
    """
    return {"approval": ApprovalGate()}
