"""Model data aplikasi (dataclass + konstanta tahapan pipeline)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Urutan stage pipeline. Orkestrator menjalankannya berurutan dan mendukung resume.
STAGE_ORDER: tuple[str, ...] = (
    "research",
    "script",
    "review",
    "scenes",
    "assets",
    "tts",
    "subtitles",
    "render",
    "qa",
    "preview",
    "approval",
    "upload",
    "schedule",
)

STAGE_LABELS_ID: dict[str, str] = {
    "research": "Riset fakta & sumber",
    "script": "Penulisan naskah",
    "review": "Review naskah",
    "scenes": "Storyboard / scene planning",
    "assets": "Akuisisi aset visual",
    "tts": "Voice-over",
    "subtitles": "Subtitle tersinkron",
    "render": "Rendering video",
    "qa": "Pemeriksaan kualitas",
    "preview": "Pratinjau",
    "approval": "Persetujuan pengguna",
    "upload": "Upload YouTube (private)",
    "schedule": "Penjadwalan publikasi",
}


class StageStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"      # menunggu aksi manusia (mis. approval)
    SKIPPED = "skipped"


class JobStatus:
    CREATED = "created"
    RUNNING = "running"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"


def utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    topic: str
    mode: str
    status: str
    dedupe_key: str
    created_at: str
    updated_at: str
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    last_error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageRecord:
    job_id: str
    stage: str
    status: str
    attempt: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventRecord:
    id: int
    job_id: Optional[str]
    ts: str
    level: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageRecord:
    """Estimasi pemakaian per provider per job (bukan tagihan resmi)."""

    id: int
    job_id: str
    provider: str
    model: str
    kind: str                              # llm | tts | image | video | search | fetch | stt
    units: dict[str, float] = field(default_factory=dict)
    cost_estimate: float = 0.0             # perkiraan berbasis metadata provider
    ts: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactSource:
    """Satu fakta beserta dukungan sumbernya (dipakai Fase C).

    `confidence` = keyakinan model terhadap fakta (high|medium|low).
    `verification` = apakah fakta sudah DICEK otomatis terhadap sumber:
        fetched_supported | source_provided_unfetched | no_source | fetch_failed | synthetic_only
    """

    claim: str
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    accessed_at: Optional[str] = None
    confidence: str = "medium"
    verification: str = "no_source"
    quote: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    provider: str = ""
    published: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MediaAsset:
    """Metadata aset visual/audio: sumber, lisensi, atribusi."""

    kind: str                               # image | stock_video | generated_image | generated_video | procedural
    path: str
    source: str                             # provider/asal
    license: str = "unknown"
    attribution: Optional[str] = None
    usage_allowed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScriptScene:
    index: int
    narration: str
    visual_prompt: str
    duration_hint_s: float = 0.0
    on_screen_text: Optional[str] = None
    assets: list[MediaAsset] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
