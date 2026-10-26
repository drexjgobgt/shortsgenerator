"""shortsgen - generator YouTube Shorts otomatis (9Router + FFmpeg + YouTube API).

Modul utama:
- config          : pemuatan & validasi konfigurasi .env
- logging_setup   : logging konsol + JSONL dengan redaksi rahasia
- repository      : SQLite job repository (state machine, resume, dedupe, usage)
- providers       : kontrak provider + adapter 9Router + mock
- pipeline        : orkestrator stage dengan checkpoint/resume
- cli             : antarmuka baris perintah
"""

__version__ = "0.1.0"
