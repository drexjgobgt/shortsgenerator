# shortsgen

Generator YouTube Shorts lokal berbasis Python untuk Windows. Alurnya: topik → riset/naskah → storyboard → aset visual → narasi → subtitle → render vertikal → QA → pratinjau → persetujuan → upload melalui YouTube Data API v3.

**Status:** pipeline produksi, evaluasi, dan upload resumable sudah berjalan. Tahap `schedule`/`publishAt` **belum diimplementasikan**. Upload tidak otomatis: default **private** dan selalu membutuhkan persetujuan per job; `public`/`unlisted` hanya jika dipilih eksplisit saat approval. Video memakai gerak kamera 2.5D pada gambar, **bukan** animasi 3D generatif.

## Mulai cepat: mock/offline

Prasyarat: Python 3.10+, FFmpeg dan ffprobe (di `PATH` atau atur `FFMPEG_PATH`).

```powershell
git clone https://github.com/drexjgobgt/shortsgenerator.git
cd shortsgenerator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m shortsgen init
python -m shortsgen doctor
python -m shortsgen demo --topic "Mengapa langit Mars berbeda dari Bumi?"
python -m shortsgen list
python -m pytest
```

`APP_MODE=mock` adalah default; contoh ini tidak membutuhkan API key atau OAuth. Hasil job tersimpan di `DATA_DIR` (default `./data`), log di `LOGS_DIR` (default `./logs`). Untuk render 1080×1920, atur `RENDER_PRESET=final` di `.env`; `fast` cocok untuk uji cepat. `doctor` memeriksa konfigurasi lokal; gunakan opsi `--mode live` hanya jika provider sudah dikonfigurasi.

## Mode live dan publikasi

1. Jalankan **9Router** secara lokal; atur `NINE_ROUTER_BASE_URL` dan `NINE_ROUTER_API_KEY` di `.env`. Sebagai alternatif lokal, `NINE_ROUTER_API_KEY_FROM_LOCAL_DB=true` membaca key dari instalasi 9Router milik Anda. Pilih model dan provider yang tersedia untuk akun Anda; model gratis/kuota dapat berubah. Tanpa konfigurasi provider search/fetch, verifikasi sumber dilakukan manual — jangan menganggap klaim tanpa sumber sudah terbukti.
2. Untuk upload, pasang `python -m pip install -e ".[youtube]"`, aktifkan YouTube Data API v3, buat OAuth **Desktop app**, dan simpan hasil unduhan sebagai `client_secrets.json` di akar proyek (atau set `YOUTUBE_CLIENT_SECRETS`). Login dengan `python -m shortsgen auth`. Token OAuth default disimpan di `<DATA_DIR>/auth/token.json`.
3. Jalankan job, periksa `artifacts/` dan preview, lalu setujui **hanya** jika konten, audiens, dan visibilitasnya sudah benar:

```powershell
python -m shortsgen create --topic "Kenapa jari keriput di air?" --mode live
python -m shortsgen run --job <job_id> --mode live
python -m shortsgen status --job <job_id>
# Setelah meninjau preview: pilih satu opsi sesuai persetujuan Anda.
python -m shortsgen approve --job <job_id> --mode live
# ATAU, hanya jika Anda memang ingin publik:
python -m shortsgen approve --job <job_id> --mode live --visibility public
python -m shortsgen run --job <job_id> --mode live
```

`approve` tanpa `--visibility` menggunakan default private. Jangan jalankan kedua perintah approval sekaligus. Set `YOUTUBE_MADE_FOR_KIDS` sesuai audiens sebenarnya; nilai contoh `false` ditujukan untuk audiens umum, bukan untuk konten yang memang menyasar anak-anak. Kategori, suara, dan disclosure konten sintetis perlu ditinjau sebelum upload. Pipeline memeriksa duplikat (fingerprint video), menggunakan OAuth resmi dan menunggu pemrosesan YouTube.

## Fitur utama

- Story arc dengan hook, konteks, eskalasi, fakta, dan payoff; tipe klaim `verified`, `theory`, `speculation`, atau `dramatization`.
- Style bible dan storyboard per scene; gambar AI/stock berlisensi (bila dikonfigurasi), relevansi visual heuristik atau vision opsional.
- TTS `edge-tts` lokal sebagai pilihan dasar; impor audio per scene dengan manifest dan hash SHA-256 untuk voice dari provider lain. Model, biaya, dan hak penggunaan provider eksternal harus diperiksa sendiri sebelum digunakan.
- FFmpeg: video vertikal H.264/AAC, gerak kamera 2.5D, transisi, musik/SFX prosedural, subtitle karaoke, pengukuran audio, QA serta laporan evaluasi.
- Cache hasil provider yang sukses, checkpoint/resume per stage, pembatasan biaya, dan persetujuan wajib sebelum upload.

Perintah tambahan: `python -m shortsgen --help`, `status`, `list`, `rerun`, `evaluate`, `storyboard`, `compare`, dan `voice-sample`. Pemeriksaan voice opsional: `python -m pip install -e ".[voice-review]"` (mengunduh model ASR lokal); paket `[qr-demo]` menambah pustaka untuk eksperimen QR.

## Keamanan dan batasan

- **Jangan commit** `.env`, `client_secrets.json`, token OAuth, key provider, database, log, artefak job, atau video. Semua diabaikan oleh `.gitignore`; simpan data runtime di folder lokal milik Anda. `.env.example` berisi placeholder kosong, bukan kredensial.
- Provider berbayar tidak diaktifkan secara default (`ALLOW_PAID_PROVIDERS=false`). Biaya/kuota akun eksternal perlu diperiksa sebelum mengaktifkannya.
- Tanpa sumber yang dapat dicek, klaim tidak boleh dipresentasikan sebagai fakta terverifikasi. Visual AI/diagram adalah ilustrasi, bukan bukti eksperimen.
- Tidak ada video generatif gratis yang dijamin tersedia; default fallback berupa gambar/gerak kamera FFmpeg. Tidak ada penjadwalan `publishAt` pada versi ini.
- Retensi penonton dan CTR Shorts tidak dapat disimpulkan dari QA lokal atau YouTube Data API; ukur di YouTube Studio.

Kode inti ada di `shortsgen/`; pengujian di `tests/`. Contoh konfigurasi lengkap: [`.env.example`](.env.example). Repositori publik ini sengaja tidak memuat catatan produksi pribadi, media contoh, atau kredensial.