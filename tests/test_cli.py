"""Test CLI end-to-end dalam mode mock (tanpa jaringan)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from shortsgen import cli


def write_env(tmp_path: Path, extra: str = "") -> Path:
    from conftest import ffmpeg_env

    env_values = {
        "APP_MODE": "mock",
        "DATA_DIR": str(tmp_path / "data"),
        "LOGS_DIR": str(tmp_path / "logs"),
        # render kecil agar pengujian CLI cepat
        "RENDER_PRESET": "fast",
        "RENDER_WIDTH": "320",
        "RENDER_HEIGHT": "576",
        **ffmpeg_env(),
    }
    env = tmp_path / ".env.test"
    env.write_text(
        "\n".join(f"{k}={v}" for k, v in env_values.items()) + "\n" + extra,
        encoding="utf-8",
    )
    return env


def test_init_creates_dirs_and_db(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    code = cli.main(["init", "--env-file", str(env)])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Inisialisasi selesai" in out
    assert (tmp_path / "data" / "shortsgen.sqlite").exists()
    assert (tmp_path / "logs").exists()


def test_doctor_mock_mode_json(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    code = cli.main(["doctor", "--env-file", str(env), "--json"])
    assert code == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["config"]["mode"] == "mock"
    checks = {c["name"] for c in report["checks"]}
    assert {"python", "data_dir_writable", "database", "ffmpeg"} <= checks
    # mode mock: provider tersedia offline; video not_configured (fallback FFmpeg)
    statuses = {p["capability"]: p["status"] for p in report["providers"]}
    assert statuses["llm"] == "available"
    assert statuses["tts"] == "available"
    assert statuses["video"] == "not_configured"
    # laporan tersimpan
    reports = list((tmp_path / "data" / "reports").glob("doctor-*.json"))
    assert len(reports) == 1


def test_doctor_config_error_exit_code(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env.bad"
    env.write_text(
        "APP_MODE=live\n"
        f"DATA_DIR={tmp_path / 'd'}\n"
        f"LOGS_DIR={tmp_path / 'l'}\n",
        encoding="utf-8",
    )
    code = cli.main(["doctor", "--env-file", str(env)])
    assert code == cli.EXIT_CONFIG
    out = capsys.readouterr().out
    assert "KONFIGURASI TIDAK VALID" in out
    assert "NINE_ROUTER_API_KEY" in out


def test_create_list_status_dedupe(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    code = cli.main(["create", "--env-file", str(env), "--topic", "Mengapa langit Mars berbeda?"])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    match = re.search(r"Job dibuat: (job_\w+)", out)
    assert match, out
    job_id = match.group(1)

    # duplikat ditolak
    code2 = cli.main(["create", "--env-file", str(env), "--topic", "  mengapa   LANGIT mars berbeda?  "])
    assert code2 == cli.EXIT_ERROR
    assert "DUPLIKAT" in capsys.readouterr().out

    # force diizinkan
    code3 = cli.main(["create", "--env-file", str(env), "--topic", "Mengapa langit Mars berbeda?", "--force"])
    assert code3 == cli.EXIT_OK

    code4 = cli.main(["list", "--env-file", str(env)])
    assert code4 == cli.EXIT_OK
    assert "langit Mars" in capsys.readouterr().out

    code5 = cli.main(["status", "--env-file", str(env), "--job", job_id])
    assert code5 == cli.EXIT_OK
    status_out = capsys.readouterr().out
    assert "resume berikutnya: research" in status_out
    assert "pending" in status_out


def test_demo_runs_full_mock_pipeline(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    code = cli.main(["demo", "--env-file", str(env), "--topic", "Fakta unik tentang Mars"])
    assert code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Job demo:" in out
    assert "done" in out

    # artefak stage nyata (Fase C) dibuat di disk
    job_id = re.search(r"Job demo: (job_\w+)", out).group(1)
    artifacts = tmp_path / "data" / "jobs" / job_id / "artifacts"
    assert (artifacts / "research.json").exists()
    assert (artifacts / "script.json").exists()
    assert (artifacts / "storyboard.json").exists()

    # status menampilkan pemakaian LLM mock
    cli.main(["status", "--env-file", str(env), "--job", job_id])
    status_out = capsys.readouterr().out
    assert "mock:llm" in status_out
    assert "<tidak ada (selesai)>" in status_out


def test_run_resumes_after_interruption(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    cli.main(["create", "--env-file", str(env), "--topic", "Topik untuk resume"])
    job_id = re.search(r"Job dibuat: (job_\w+)", capsys.readouterr().out).group(1)

    code = cli.main(["run", "--env-file", str(env), "--job", job_id, "--upto", "review"])
    assert code == cli.EXIT_OK

    code2 = cli.main(["run", "--env-file", str(env), "--job", job_id])
    assert code2 == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "dilewati" in out


def test_status_unknown_job(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    code = cli.main(["status", "--env-file", str(env), "--job", "job_tidak_ada"])
    assert code == cli.EXIT_ERROR
    assert "ERROR" in capsys.readouterr().out


def test_probe_requires_live_mode(tmp_path: Path, capsys) -> None:
    env = write_env(tmp_path)
    code = cli.main(["probe", "--env-file", str(env)])
    assert code == cli.EXIT_CONFIG
    assert "mode live" in capsys.readouterr().out.lower()
