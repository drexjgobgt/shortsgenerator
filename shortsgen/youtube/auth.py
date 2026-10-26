"""OAuth 2.0 resmi YouTube untuk aplikasi Desktop.

Alur:
1. `client_secrets.json` (tipe Desktop) dibaca dari disk.
2. Browser dibuka sekali untuk consent; redirect memakai loopback localhost.
3. Token akses + refresh disimpan ke `token_path` dengan izin ketat.
4. Pemanggilan berikutnya memakai refresh token; login ulang hanya bila token
   dicabut/kedaluwarsa.

Tidak ada password Google yang disimpan. Isi token tidak pernah dilog.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from ..errors import ShortsGenError

SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)


class YouTubeAuthError(ShortsGenError):
    """Kredensial/token YouTube tidak tersedia atau tidak valid."""


def _google_imports() -> tuple[Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - hanya bila dependensi belum ada
        raise YouTubeAuthError(
            "Dependensi Google belum terpasang. Jalankan: "
            "python -m pip install google-api-python-client google-auth-oauthlib"
        ) from exc
    return Request, Credentials, InstalledAppFlow


def validate_client_secrets(path: Path) -> dict[str, Any]:
    """Validasi struktur client_secrets.json tanpa mencetak isi rahasia."""
    path = Path(path)
    if not path.exists():
        raise YouTubeAuthError(
            f"client_secrets.json tidak ditemukan di {path}. Unduh OAuth client tipe Desktop "
            "dari Google Cloud Console lalu simpan dengan nama itu."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YouTubeAuthError(f"client_secrets.json tidak bisa dibaca: {exc}") from exc
    if "installed" not in payload:
        raise YouTubeAuthError(
            "client_secrets.json bukan OAuth client tipe Desktop. "
            "Buat kredensial 'Desktop app', bukan 'Web application'."
        )
    installed = payload.get("installed") or {}
    if not installed.get("client_id") or not installed.get("client_secret"):
        raise YouTubeAuthError("client_secrets.json tidak memuat client_id/client_secret.")
    return payload


def _save_token(credentials: Any, token_path: Path) -> None:
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        # Windows tidak selalu mendukung chmod; file tetap berada di folder data lokal.
        pass


def load_credentials(
    client_secrets: Path,
    token_path: Path,
    *,
    interactive: bool = True,
    force: bool = False,
    logger: Any = None,
) -> Any:
    """Muat token yang ada atau jalankan consent OAuth sekali.

    Raises:
        YouTubeAuthError: kredensial tidak ada/tidak valid dan `interactive=False`.
    """
    Request, Credentials, InstalledAppFlow = _google_imports()
    token_path = Path(token_path)

    if not force and token_path.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), list(SCOPES))
        except (OSError, ValueError, json.JSONDecodeError):
            credentials = None
        if credentials is not None and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
                _save_token(credentials, token_path)
            except Exception:  # noqa: BLE001 - token kedaluwarsa/dicabut -> login ulang
                credentials = None
        if credentials is not None and credentials.valid:
            if logger is not None:
                logger.info("Token OAuth YouTube dimuat dari %s", token_path)
            return credentials

    if not interactive:
        raise YouTubeAuthError(
            "Token OAuth YouTube tidak tersedia/kedaluwarsa. Jalankan "
            "`python -m shortsgen auth` untuk login."
        )

    validate_client_secrets(client_secrets)
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(SCOPES))
    if logger is not None:
        logger.info("Membuka browser untuk consent OAuth YouTube (sekali saja)...")
    credentials = flow.run_local_server(
        port=0,
        open_browser=True,
        authorization_prompt_message="Buka URL ini untuk memberi izin YouTube: {url}",
        success_message="OAuth YouTube berhasil. Anda boleh menutup tab ini.",
        prompt="select_account consent",
    )
    _save_token(credentials, token_path)
    if logger is not None:
        logger.info("Token OAuth YouTube disimpan di %s", token_path)
    return credentials
