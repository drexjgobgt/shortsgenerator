"""Narrative engine: cerita hiburan yang tetap jujur pada fakta.

Modul ini mengubah keluaran LLM menjadi struktur `beats` yang kaya arahan:
- peran naratif (hook/context/escalation/evidence/twist/payoff/closing),
- jenis klaim (verified/theory/speculation/dramatization),
- emosi + intensitas untuk voice acting,
- kata yang perlu ditekankan,
- seed visual konkret + isyarat SFX/transisi.

Prinsip:
- `claim_type` TIDAK boleh menaikkan keyakinan fakta. Nilainya diturunkan dari
  metadata fakta riset (confidence + verification), bukan dari klaim LLM.
- Dramatisasi diperbolehkan untuk menghibur, tetapi wajib punya catatan dan
  tidak boleh menjadi satu-satunya dasar klaim faktual.
- Struktur lama (`blocks`) tetap dihasilkan agar stage lama tetap kompatibel.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Kecepatan bicara nyata edge-tts Indonesia (kalibrasi job live: ~1,9 kata/dtk).
WORDS_PER_SECOND = 2.0
MIN_BEATS = 8
MAX_BEATS = 12
MAX_HOOK_WORDS = 12
MAX_BEAT_WORDS = 16
MAX_CLOSING_WORDS = 16

VALID_ROLES = ("hook", "context", "escalation", "evidence", "twist", "payoff", "closing")
VALID_CLAIMS = ("verified", "theory", "speculation", "dramatization")
VALID_EMOTIONS = (
    "curious",
    "excited",
    "suspenseful",
    "surprised",
    "conversational",
    "comedic",
    "serious",
)
VALID_SFX = ("none", "whoosh", "impact", "riser", "heartbeat", "sparkle", "sub_drop")
VALID_TRANSITIONS = ("cut", "fade", "smoothleft", "smoothright", "circleopen", "zoomin", "wipeup")

BANNED_OPENINGS = (
    "tahukah kamu",
    "tahukah kalian",
    "hai",
    "halo",
    "selamat datang",
    "di video kali ini",
    "pada video kali ini",
    "kali ini kita",
    "jangan lupa like",
)


@dataclass(frozen=True)
class HookPattern:
    id: str
    name: str
    instruction: str
    example: str
    best_for: tuple[str, ...]


HOOK_PATTERNS: tuple[HookPattern, ...] = (
    HookPattern(
        "contradiction",
        "Kontradiksi",
        "Buka dengan pertentangan: sesuatu yang tampak X ternyata justru Y.",
        "Pesawat seberat ratusan ton justru bisa melayang lebih mudah saat makin cepat.",
        ("science", "technology", "space", "nature", "animals"),
    ),
    HookPattern(
        "impossible_number",
        "Angka mustahil",
        "Buka dengan angka mengejutkan yang spesifik, lalu janjikan penjelasannya.",
        "Satu makhluk ini punya tiga jantung, dan salah satunya berhenti saat ia berenang.",
        ("animals", "science", "body", "history", "trivia"),
    ),
    HookPattern(
        "myth_bust",
        "Bongkar mitos",
        "Sebut keyakinan umum yang salah, lalu balik dalam satu kalimat.",
        "Kita diajari bahwa darah selalu merah. Di laut, ada makhluk dengan darah biru.",
        ("science", "animals", "history", "food", "psychology"),
    ),
    HookPattern(
        "question",
        "Pertanyaan spesifik",
        "Ajukan pertanyaan yang tidak bisa dijawab dengan 'ya/tidak' dan terasa mendesak.",
        "Kalau jantungnya berhenti saat berenang, bagaimana gurita bisa tetap hidup?",
        ("science", "space", "technology", "nature", "body"),
    ),
    HookPattern(
        "cold_open_story",
        "Cerita dingin",
        "Mulai di tengah adegan konkret, seolah penonton sudah berada di sana.",
        "Di kedalaman 800 meter, seekor gurita melepaskan lengannya sendiri lalu pergi.",
        ("animals", "history", "weird", "nature", "trivia"),
    ),
    HookPattern(
        "discovery",
        "Penemuan",
        "Buka dengan penemuan/observasi yang terdengar baru, tanpa mengarang sumber.",
        "Para peneliti menemukan bahwa lengan gurita bisa 'mengambil keputusan' sendiri.",
        ("science", "history", "technology", "space", "psychology"),
    ),
    HookPattern(
        "comparison",
        "Perbandingan",
        "Bandingkan dua hal yang akrab dengan sesuatu yang ekstrem.",
        "Manusia punya satu jantung. Gurita punya tiga, dan darahnya berwarna biru.",
        ("animals", "science", "body", "technology", "trivia"),
    ),
    HookPattern(
        "stakes",
        "Taruhan",
        "Buka dengan konsekuensi: kalau ini salah, ada hal besar yang berubah.",
        "Kalau gurita berenang terlalu lama, jantung utamanya berhenti. Itu bukan metafora.",
        ("science", "animals", "technology", "history", "body"),
    ),
    HookPattern(
        "sensory",
        "Imajinasi indrawi",
        "Ajak penonton membayangkan sensasi fisik yang aneh/spesifik.",
        "Bayangkan punya tiga jantung, darah biru, dan delapan lengan yang bisa berpikir sendiri.",
        ("animals", "weird", "space", "body", "nature"),
    ),
    HookPattern(
        "countdown",
        "Hitungan",
        "Buka dengan hitungan yang menjanjikan payoff di akhir.",
        "Ada tiga hal aneh tentang gurita. Yang ketiga membuatnya sulit disebut hewan biasa.",
        ("trivia", "animals", "history", "science", "pop_culture"),
    ),
)

ARCHETYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "space": ("mars", "planet", "antariksa", "astronomi", "bintang", "galaksi", "bulan", "matahari", "space", "nasa"),
    "animals": ("gurita", "octopus", "hewan", "animal", "burung", "ikan", "serangga", "kucing", "anjing", "paus", "laba-laba", "lebah"),
    "body": ("otak", "jantung", "darah", "tubuh", "manusia", "tidur", "mimpi", "mata", "kulit", "dna", "gen"),
    "history": ("sejarah", "perang", "kerajaan", "kuno", "abad", "mesir", "romawi", "penemuan", "historis"),
    "technology": ("teknologi", "komputer", "internet", "mesin", "robot", "listrik", "chip", "pesawat", "kode"),
    "food": ("makanan", "madu", "kopi", "cokelat", "resep", "kuliner", "buah", "dapur", "gula", "garam"),
    "psychology": ("psikologi", "perasaan", "kebiasaan", "ingatan", "stress", "bahagia", "takut"),
    "nature": ("hujan", "pelangi", "gunung", "laut", "samudra", "cuaca", "petir", "hutan", "alam"),
    "weird": ("aneh", "unik", "teraneh", "bizarre", "misteri", "fenomena"),
    "pop_culture": ("film", "musik", "game", "selebriti", "meme", "anime", "kartun", "pop"),
}

# Prioritas saat skor imbang: subjek utama lebih penting daripada kata pendukung.
ARCHETYPE_PRIORITY: dict[str, int] = {
    "animals": 10,
    "space": 9,
    "history": 8,
    "technology": 7,
    "food": 6,
    "psychology": 5,
    "nature": 4,
    "weird": 3,
    "body": 2,
    "pop_culture": 1,
    "trivia": 0,
}


def classify_topic(topic: str) -> str:
    """Tebak arketipe topik dari kata kunci (deterministik, bukan LLM).

    Pencocokan memakai token utuh agar kata pendek seperti "ai"/"es" tidak
    cocok di tengah kata lain (mis. "bagaimana", "sesuatu").
    """
    lowered = topic.lower()
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    scores: dict[str, int] = {}
    for archetype, keywords in ARCHETYPE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if " " in keyword:
                if keyword in lowered:
                    score += 1
            elif keyword in tokens:
                score += 1
        scores[archetype] = score
    best = max(scores, key=lambda key: (scores[key], ARCHETYPE_PRIORITY.get(key, 0)))
    return best if scores[best] > 0 else "trivia"


def select_hook_pattern(topic: str, archetype: str, *, seed: int | None = None) -> HookPattern:
    """Pilih pola hook yang cocok; seed membuat pilihan stabil per job."""
    fitting = [pattern for pattern in HOOK_PATTERNS if archetype in pattern.best_for]
    if not fitting:
        fitting = list(HOOK_PATTERNS)
    if seed is None:
        digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()
        seed = int(digest[:8], 16)
    return fitting[seed % len(fitting)]


def build_narrative_prompt(
    *,
    topic: str,
    facts: list[dict[str, Any]],
    target_duration: int,
    min_beats: int = MIN_BEATS,
    max_beats: int = MAX_BEATS,
    archetype: str,
    hook_pattern: HookPattern,
    series_character: str = "",
) -> str:
    """Prompt naskah hiburan-first dengan skema beats + arahan produksi."""
    facts_block = _render_facts(facts)
    target_words = int(target_duration * WORDS_PER_SECOND)
    character_line = (
        f"\nKarakter seri (opsional, hanya jika relevan): {series_character}\n"
        if series_character
        else ""
    )
    return f"""Kamu adalah penulis naskah YouTube Shorts hiburan-fakta berbahasa Indonesia.
Gaya: storyteller yang santai, cerdas, sedikit jenaka — BUKAN pembaca berita, BUKAN presentasi slide.

Topik: {topic}
Arketipe: {archetype}
Durasi target: {target_duration} detik (± 15%)
Target kata narasi total: sekitar {target_words} kata (jangan lebih dari {int(target_words * 1.2)} kata)
Jumlah beat: {min_beats}-{max_beats} (hook + isi + closing)
Pola hook yang disarankan: {hook_pattern.name} — {hook_pattern.instruction}
Contoh pola (jangan menyalin mentah): {hook_pattern.example}
{character_line}
Daftar fakta yang BOLEH dipakai (rujuk id-nya):
{facts_block}

STRUKTUR WAJIB:
1. HOOK (maks {MAX_HOOK_WORDS} kata): langsung masuk ke hal mengejutkan/kontradiksi/angka.
   DILARANG: "tahukah kamu", "hai", "halo", "selamat datang", "di video kali ini", basa-basi.
2. CONTEXT (1 beat): beri konteks minimum agar penonton paham taruhannya.
3. ESCALATION (2-3 beat): tambah detail yang makin menarik; buka "open loop" di awal.
4. EVIDENCE (2-3 beat): fakta utama + mekanisme/penjelasan. Gunakan angka/kontras.
5. TWIST/PAYOFF (1-2 beat): kejutan yang tetap berdasar fakta; jawab open loop.
6. CLOSING (maks {MAX_CLOSING_WORDS} kata): punchline yang menyambung kembali ke hook
   supaya video terasa "loop" saat diputar ulang.

ATURAN FAKTA (WAJIB):
- Pakai HANYA fakta dari daftar. Jangan menambah fakta baru dari luar daftar.
- `claim_type` tiap beat: "verified" hanya jika fakta pendukung berstatus fetched_supported;
  selain itu "theory" (fakta mapan tapi belum diverifikasi di sini), "speculation" (keyakinan rendah),
  atau "dramatization" (gaya bercerita, bukan klaim fakta baru).
- Untuk "dramatization", isi `dramatization_note` yang menjelaskan bagian mana yang dramatisasi.
- Jangan mengarang URL/sumber. Jangan memberi nasihat medis/keuangan/hukum.
- Jangan clickbait menyesatkan. Kejutan harus benar.

ARAH PRODUKSI tiap beat:
- `emotion`: curious|excited|suspenseful|surprised|conversational|comedic|serious
- `intensity`: 1-5 (1 tenang, 5 puncak)
- `emphasis`: 1-3 kata/frasa PENTING yang benar-benar ada di narasi (untuk penekanan subtitle/voice)
- `visual_seed`: 5-15 kata BAHASA INGGRIS, konkret: subjek + aksi + latar yang bisa digambar.
  Hindari kata abstrak ("concept", "mysterious", "cinematic") tanpa subjek.
- `sfx_cue`: none|whoosh|impact|riser|heartbeat|sparkle|sub_drop
- `transition`: cut|fade|smoothleft|smoothright|circleopen|zoomin|wipeup
- `voice_direction`: arahan singkat bahasa Indonesia (mis. "pelan, seperti membocorkan rahasia")
- `purpose`: fungsi beat ini dalam cerita (1 frasa)

Balas HANYA JSON valid (tanpa markdown fence, tanpa penjelasan):
{{
  "title": "judul maksimal 60 karakter, kata kunci di depan",
  "logline": "satu kalimat tentang cerita video",
  "hook": "kalimat pembuka <= {MAX_HOOK_WORDS} kata",
  "hook_pattern": "{hook_pattern.id}",
  "hook_variants": ["2-3 alternatif hook dengan sudut berbeda"],
  "promise": "hal yang dijanjikan di awal",
  "stakes": "kenapa ini penting/menarik",
  "beats": [
    {{
      "role": "hook|context|escalation|evidence|twist|payoff|closing",
      "narration": "6-12 kata, satu ide",
      "on_screen_text": "maks 5 kata",
      "fact_refs": ["F1"],
      "claim_type": "verified|theory|speculation|dramatization",
      "dramatization_note": "wajib bila claim_type=dramatization",
      "emotion": "curious",
      "intensity": 3,
      "emphasis": ["kata penting"],
      "visual_seed": "english concrete visual subject + action + setting",
      "sfx_cue": "whoosh",
      "transition": "smoothleft",
      "voice_direction": "santai, sedikit penasaran",
      "purpose": "membuka rasa penasaran"
    }}
  ],
  "closing": "kalimat penutup <= {MAX_CLOSING_WORDS} kata",
  "description": "2-3 kalimat + 2-4 hashtag relevan",
  "tags": ["8-12 kata kunci"],
  "facts_used": ["F1", "F2"],
  "disclosure": "catatan bahwa video memakai narasi/visual sintetis"
}}"""


def normalize_narrative(
    data: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    topic: str,
    target_duration: int,
    archetype: str | None = None,
    hook_pattern: HookPattern | None = None,
) -> dict[str, Any]:
    """Normalisasi keluaran LLM (baru atau lama) menjadi script.json baku."""
    archetype = archetype or classify_topic(topic)
    hook_pattern = hook_pattern or select_hook_pattern(topic, archetype)

    title = str(data.get("title") or "").strip()
    hook = str(data.get("hook") or "").strip()
    closing = str(data.get("closing") or "").strip()
    raw_beats = data.get("beats")
    raw_scenes = data.get("scenes") or []

    if not hook:
        raise RuntimeError("naskah tidak memuat hook")
    if not isinstance(raw_beats, list) or not raw_beats:
        raw_beats = _beats_from_legacy(hook, raw_scenes, closing)
    if not raw_beats:
        raise RuntimeError("naskah tidak memuat beat yang cukup")

    known_ids = {f"F{index}" for index in range(1, len(facts) + 1)}
    fact_meta = {f"F{index}": fact for index, fact in enumerate(facts, start=1)}

    beats: list[dict[str, Any]] = []
    unknown_refs: set[str] = set()
    for position, raw in enumerate(raw_beats):
        if not isinstance(raw, dict):
            continue
        beat = _normalize_beat(
            raw,
            position=position,
            known_ids=known_ids,
            fact_meta=fact_meta,
            topic=topic,
        )
        if beat is None:
            continue
        unknown_refs.update(beat.pop("_unknown_refs", []))
        beats.append(beat)

    if len(beats) < 3:
        raise RuntimeError("naskah menghasilkan terlalu sedikit beat narasi")

    # pastikan hook dan closing ada di posisi yang benar
    if beats[0]["role"] != "hook":
        beats[0]["role"] = "hook"
    if beats[-1]["role"] not in ("closing", "payoff"):
        beats[-1]["role"] = "closing"

    blocks = [_beat_to_block(beat, index) for index, beat in enumerate(beats)]
    total_words = sum(beat["word_count"] for beat in beats)
    estimated = round(total_words / WORDS_PER_SECOND, 1)

    declared_facts = [str(item).strip().upper() for item in (data.get("facts_used") or []) if str(item).strip()]
    beat_facts = [ref for beat in beats for ref in beat.get("fact_refs", [])]
    facts_used = sorted(set(declared_facts) | set(beat_facts), key=_fact_sort_key)
    unknown_refs.update(ref for ref in declared_facts if ref not in known_ids)

    hook_variants = [str(item).strip() for item in (data.get("hook_variants") or []) if str(item).strip()][:3]
    if hook and hook not in hook_variants:
        hook_variants.insert(0, hook)

    pattern_id = str(data.get("hook_pattern") or hook_pattern.id).strip().lower()
    if pattern_id not in {pattern.id for pattern in HOOK_PATTERNS}:
        pattern_id = hook_pattern.id

    emotions = {beat["emotion"] for beat in beats}
    claim_counts = {claim: sum(1 for beat in beats if beat["claim_type"] == claim) for claim in VALID_CLAIMS}
    arc_roles = [beat["role"] for beat in beats]

    return {
        "title": title[:120] or topic[:80],
        "logline": str(data.get("logline") or "").strip(),
        "hook": hook,
        "hook_pattern": pattern_id,
        "hook_variants": hook_variants,
        "promise": str(data.get("promise") or "").strip(),
        "stakes": str(data.get("stakes") or "").strip(),
        "closing": closing or beats[-1]["narration"],
        "loop_line": closing or beats[-1]["narration"],
        "description": str(data.get("description") or "").strip(),
        "tags": [str(item).strip() for item in (data.get("tags") or []) if str(item).strip()][:15],
        "disclosure": str(data.get("disclosure") or "").strip()
        or "Video memakai narasi suara sintetis dan visual yang dihasilkan/diproses secara digital.",
        "facts_used": facts_used,
        "unknown_fact_refs": sorted(unknown_refs),
        "sources": [
            {
                "id": ref,
                "claim": fact_meta[ref].get("claim", ""),
                "confidence": fact_meta[ref].get("confidence", "unverified"),
                "verification": fact_meta[ref].get("verification", "no_source"),
                "source_url": fact_meta[ref].get("source_url"),
                "source_title": fact_meta[ref].get("source_title"),
            }
            for ref in facts_used
            if ref in fact_meta
        ],
        "narrative": {
            "archetype": archetype,
            "hook_pattern": pattern_id,
            "arc_roles": arc_roles,
            "emotion_variety": len(emotions),
            "claim_type_counts": claim_counts,
            "target_duration_s": target_duration,
        },
        "beats": beats,
        "blocks": blocks,
        "stats": {
            "total_words": total_words,
            "estimated_duration_s": estimated,
            "block_count": len(beats),
            "beat_count": len(beats),
            "avg_block_words": round(total_words / max(1, len(beats)), 1),
            "hook_words": len(hook.split()),
            "has_rhetorical_question": "?" in " ".join(beat["narration"] for beat in beats),
            "emotion_variety": len(emotions),
            "claim_types": claim_counts,
            "emphasis_count": sum(len(beat.get("emphasis", [])) for beat in beats),
        },
        "created_at": _utcnow(),
    }


def validate_narrative(script: dict[str, Any]) -> list[dict[str, str]]:
    """Aturan hiburan + kejujuran fakta. Mengembalikan temuan (severity, code, pesan)."""
    issues: list[dict[str, str]] = []

    def add(severity: str, code: str, message: str) -> None:
        issues.append({"severity": severity, "code": code, "message": message})

    hook = str(script.get("hook") or "").strip()
    hook_lower = hook.lower()
    if not hook:
        add("high", "missing_hook", "hook kosong")
    else:
        hook_words = len(hook.split())
        if hook_words > MAX_HOOK_WORDS + 4:
            add("high", "hook_too_long", f"hook {hook_words} kata (maks {MAX_HOOK_WORDS})")
        elif hook_words > MAX_HOOK_WORDS:
            add("medium", "hook_too_long", f"hook {hook_words} kata (maks {MAX_HOOK_WORDS})")
        for phrase in BANNED_OPENINGS:
            if hook_lower.startswith(phrase):
                add("high", "generic_opening", f"pembukaan generik dilarang: '{phrase}'")
                break
        if not _has_curiosity_trigger(hook):
            add("medium", "hook_no_curiosity", "hook tanpa pemicu rasa ingin tahu (pertanyaan/angka/kontras)")

    beats = script.get("beats") or []
    roles = [beat.get("role") for beat in beats]
    if len(beats) < MIN_BEATS:
        add("medium", "too_few_beats", f"hanya {len(beats)} beat (disarankan >= {MIN_BEATS})")
    for required in ("context", "escalation", "evidence"):
        if required not in roles:
            add("medium", f"missing_{required}", f"arc tidak memuat beat '{required}'")
    if "payoff" not in roles and "twist" not in roles:
        add("medium", "missing_payoff", "tidak ada payoff/twist yang memuaskan")
    if roles and roles[-1] not in ("closing", "payoff"):
        add("low", "weak_ending", "beat terakhir bukan closing/payoff")

    # durasi
    estimated = float((script.get("stats") or {}).get("estimated_duration_s") or 0)
    target = float((script.get("narrative") or {}).get("target_duration_s") or 30)
    if estimated and not (target * 0.6 <= estimated <= target * 1.5):
        add(
            "medium",
            "duration_vs_target",
            f"estimasi {estimated:.1f} dtk jauh dari target {target:.0f} dtk "
            f"(kecepatan bicara {WORDS_PER_SECOND:.1f} kata/dtk)",
        )

    # klaim & dramatisasi
    for beat in beats:
        claim = beat.get("claim_type")
        refs = beat.get("fact_refs") or []
        if claim not in VALID_CLAIMS:
            add("medium", "invalid_claim_type", f"beat {beat.get('index')} claim_type tidak dikenal: {claim}")
        if claim == "verified" and not _has_verified_support(refs, script):
            add(
                "high",
                "unverified_marked_verified",
                f"beat {beat.get('index')} ditandai verified tetapi fakta pendukung belum terverifikasi",
            )
        if claim == "dramatization" and not beat.get("dramatization_note"):
            add(
                "medium",
                "dramatization_without_note",
                f"beat {beat.get('index')} dramatisasi tanpa catatan",
            )
        if not refs and claim in ("verified", "theory"):
            add(
                "low",
                "missing_fact_ref",
                f"beat {beat.get('index')} berklaim {claim} tanpa merujuk fakta",
            )

    # kekayaan emosi & penekanan
    emotions = [beat.get("emotion") for beat in beats]
    if len(set(emotions)) < 3:
        add("low", "emotion_flat", "variasi emosi < 3; voice acting berpotensi datar")
    if not any(beat.get("emphasis") for beat in beats):
        add("low", "no_emphasis", "tidak ada kata yang ditekankan untuk subtitle/voice")

    # hook variants: harus punya sudut berbeda
    variants = [str(item).strip() for item in (script.get("hook_variants") or []) if str(item).strip()]
    if len(variants) < 2:
        add("low", "few_hook_variants", "kurang dari 2 varian hook untuk pengujian")
    else:
        bases = {_normalized_words(item) for item in variants[:3]}
        if len(bases) < 2:
            add("low", "hook_variants_too_similar", "varian hook terlalu mirip")

    # loop closing
    closing = str(script.get("closing") or "").strip()
    if closing:
        hook_keys = {word.lower().strip(".,!?;:") for word in hook.split() if len(word) > 4}
        closing_keys = {word.lower().strip(".,!?;:") for word in closing.split() if len(word) > 4}
        if hook_keys and not (hook_keys & closing_keys):
            add("low", "weak_loop", "closing tidak menyambung kembali ke hook (potensi loop lemah)")
    else:
        add("medium", "missing_closing", "closing kosong")

    # SFX/transisi untuk momen penting
    if not any(beat.get("sfx_cue") in ("impact", "riser", "sub_drop") for beat in beats):
        add("low", "no_impact_sfx", "tidak ada isyarat SFX untuk twist/payoff")
    if len({beat.get("transition") for beat in beats}) < 2:
        add("low", "few_transitions", "variasi transisi terlalu sedikit")

    if script.get("unknown_fact_refs"):
        add(
            "medium",
            "unknown_fact_refs",
            "naskah merujuk fakta yang tidak ada: " + ", ".join(script["unknown_fact_refs"]),
        )

    return issues


# -- helper -----------------------------------------------------------------

def _normalize_beat(
    raw: dict[str, Any],
    *,
    position: int,
    known_ids: set[str],
    fact_meta: dict[str, dict[str, Any]],
    topic: str,
) -> Optional[dict[str, Any]]:
    narration = re.sub(r"\s+", " ", str(raw.get("narration") or "")).strip()
    if not narration:
        return None
    role = str(raw.get("role") or "").strip().lower()
    if role not in VALID_ROLES:
        role = "hook" if position == 0 else "body" if position < 6 else "closing"
        if role == "body":
            role = "escalation"
    fact_refs = [str(ref).strip().upper() for ref in (raw.get("fact_refs") or []) if str(ref).strip()]
    unknown_refs = [ref for ref in fact_refs if ref not in known_ids]
    fact_refs = [ref for ref in fact_refs if ref in known_ids]

    claim = str(raw.get("claim_type") or "").strip().lower()
    if claim not in VALID_CLAIMS:
        claim = _derive_claim_type(fact_refs, fact_meta)
    if claim == "verified" and not _has_verified_support(fact_refs, {"sources": []}):
        # turunkan bila metadata fakta belum mendukung
        claim = _derive_claim_type(fact_refs, fact_meta)

    emphasis = _clean_emphasis(raw.get("emphasis"), narration)
    emotion = str(raw.get("emotion") or "").strip().lower()
    if emotion not in VALID_EMOTIONS:
        emotion = _default_emotion(role)
    try:
        intensity = int(raw.get("intensity") or 3)
    except (TypeError, ValueError):
        intensity = 3
    intensity = max(1, min(5, intensity))

    sfx = str(raw.get("sfx_cue") or "").strip().lower()
    if sfx not in VALID_SFX:
        sfx = _default_sfx(role)
    transition = str(raw.get("transition") or "").strip().lower()
    if transition not in VALID_TRANSITIONS:
        transition = _default_transition(role)

    visual_seed = re.sub(r"\s+", " ", str(raw.get("visual_seed") or raw.get("visual") or "")).strip()
    if not visual_seed:
        visual_seed = _fallback_visual_seed(narration, topic)

    beat = {
        "index": int(raw.get("index", position)),
        "role": role,
        "narration": narration,
        "on_screen_text": re.sub(r"\s+", " ", str(raw.get("on_screen_text") or "")).strip()[:48]
        or _first_short(narration),
        "fact_refs": fact_refs,
        "claim_type": claim,
        "dramatization_note": str(raw.get("dramatization_note") or "").strip() or None,
        "emotion": emotion,
        "intensity": intensity,
        "emphasis": emphasis,
        "visual_seed": visual_seed[:300],
        "sfx_cue": sfx,
        "transition": transition,
        "voice_direction": str(raw.get("voice_direction") or "").strip()
        or _default_voice_direction(emotion, intensity),
        "purpose": str(raw.get("purpose") or "").strip() or _default_purpose(role),
        "word_count": len(narration.split()),
        "_unknown_refs": unknown_refs,
    }
    return beat


def _beats_from_legacy(hook: str, scenes: list[Any], closing: str) -> list[dict[str, Any]]:
    """Bangun beat dari skema lama (hook + scenes + closing) agar kompatibel."""
    beats: list[dict[str, Any]] = []
    beats.append(
        {
            "role": "hook",
            "narration": hook,
            "on_screen_text": _first_short(hook),
            "fact_refs": [],
            "claim_type": "theory",
            "emotion": "curious",
            "intensity": 4,
            "emphasis": _auto_emphasis(hook),
            "visual_seed": "",
            "sfx_cue": "impact",
            "transition": "fade",
            "voice_direction": "langsung, sedikit menantang",
            "purpose": "menarik perhatian di detik pertama",
        }
    )
    body_scenes = [scene for scene in scenes if isinstance(scene, dict)]
    for position, scene in enumerate(body_scenes):
        if position == 0:
            role, emotion, transition = "context", "conversational", "smoothleft"
        elif position <= 2:
            role, emotion, transition = "escalation", "curious", "smoothleft"
        elif position <= len(body_scenes) - 3:
            role, emotion, transition = "evidence", "serious", "cut"
        elif position == len(body_scenes) - 2:
            role, emotion, transition = "twist", "surprised", "zoomin"
        else:
            role, emotion, transition = "payoff", "excited", "circleopen"
        narration = str(scene.get("narration") or "")
        beats.append(
            {
                "role": role,
                "narration": narration,
                "on_screen_text": str(scene.get("on_screen_text") or ""),
                "fact_refs": [f"F{position + 1}"] if position < 5 else [],
                "claim_type": "theory",
                "emotion": emotion,
                "intensity": 4 if role in ("twist", "payoff") else 3,
                "emphasis": _auto_emphasis(narration),
                "visual_seed": str(scene.get("visual_prompt") or ""),
                "sfx_cue": "riser" if role == "twist" else "whoosh",
                "transition": transition,
                "voice_direction": _default_voice_direction(emotion, 3),
                "purpose": _default_purpose(role),
            }
        )
    if closing:
        beats.append(
            {
                "role": "closing",
                "narration": closing,
                "on_screen_text": "",
                "fact_refs": [],
                "claim_type": "theory",
                "emotion": "comedic",
                "intensity": 3,
                "emphasis": _auto_emphasis(closing),
                "visual_seed": "",
                "sfx_cue": "sparkle",
                "transition": "fade",
                "voice_direction": "ringan, seperti punchline",
                "purpose": "menutup loop dan mendorong replay",
            }
        )
    return beats


def _beat_to_block(beat: dict[str, Any], index: int) -> dict[str, Any]:
    kind = "hook" if beat["role"] == "hook" else "closing" if beat["role"] == "closing" else "body"
    return {
        "index": index,
        "kind": kind,
        "role": beat["role"],
        "narration": beat["narration"],
        "on_screen_text": beat["on_screen_text"],
        "visual_prompt": beat["visual_seed"],
        "stock_query": _stock_query_from_seed(beat["visual_seed"]),
        "word_count": beat["word_count"],
        "emotion": beat["emotion"],
        "intensity": beat["intensity"],
        "emphasis": list(beat["emphasis"]),
        "fact_refs": list(beat["fact_refs"]),
        "claim_type": beat["claim_type"],
        "sfx_cue": beat["sfx_cue"],
        "transition": beat["transition"],
        "voice_direction": beat["voice_direction"],
        "purpose": beat["purpose"],
    }


def _derive_claim_type(refs: Iterable[str], fact_meta: dict[str, dict[str, Any]]) -> str:
    referenced = [fact_meta[ref] for ref in refs if ref in fact_meta]
    if not referenced:
        return "speculation"
    if any(str(fact.get("verification") or "") == "fetched_supported" for fact in referenced):
        return "verified"
    confidences = {str(fact.get("confidence") or "medium") for fact in referenced}
    if "low" in confidences:
        return "speculation"
    return "theory"


def _has_verified_support(refs: Iterable[str], script: dict[str, Any]) -> bool:
    sources = {item.get("id"): item for item in (script.get("sources") or [])}
    return any(
        (sources.get(ref) or {}).get("verification") == "fetched_supported" for ref in refs
    )


def _has_curiosity_trigger(text: str) -> bool:
    lowered = text.lower()
    markers = ("?", "justru", "ternyata", "padahal", "tapi", "bukan", "rahasia", "aneh", "mustahil")
    return any(marker in lowered for marker in markers) or any(char.isdigit() for char in text)


def _clean_emphasis(raw: Any, narration: str) -> list[str]:
    lowered = narration.lower()
    result: list[str] = []
    for item in raw or []:
        word = str(item).strip()
        if not word:
            continue
        if word.lower() in lowered and word.lower() not in {value.lower() for value in result}:
            result.append(word[:40])
        if len(result) == 3:
            break
    if not result:
        result = _auto_emphasis(narration)
    return result[:3]


def _auto_emphasis(text: str) -> list[str]:
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", text)
    if not words:
        return []
    priority: list[str] = []
    for word in words:
        lowered = word.lower()
        if any(char.isdigit() for char in word) or lowered in (
            "justru", "ternyata", "padahal", "tapi", "bukan", "tiga", "biru", "berhenti",
        ):
            priority.append(word)
    if not priority:
        candidates = [word for word in words if len(word) >= 6]
        priority = candidates[:2] or words[:1]
    return priority[:3]


def _stock_query_from_seed(seed: str) -> str:
    words = [
        word.lower().strip(".,;:!?()[]")
        for word in re.split(r"\s+", seed or "")
        if len(word) > 2
    ]
    generic = {
        "cinematic", "concept", "mysterious", "abstract", "glow", "glowing", "detailed",
        "animation", "animated", "film", "still", "render", "shot", "scene", "background",
        "beautiful", "dramatic", "light", "lighting", "style", "stylized", "color", "deep",
    }
    concrete = [word for word in words if word not in generic]
    return " ".join(concrete[:4])


def _default_emotion(role: str) -> str:
    return {
        "hook": "curious",
        "context": "conversational",
        "escalation": "curious",
        "evidence": "serious",
        "twist": "surprised",
        "payoff": "excited",
        "closing": "comedic",
    }.get(role, "conversational")


def _default_sfx(role: str) -> str:
    return {
        "hook": "impact",
        "context": "whoosh",
        "escalation": "whoosh",
        "evidence": "none",
        "twist": "riser",
        "payoff": "impact",
        "closing": "sparkle",
    }.get(role, "none")


def _default_transition(role: str) -> str:
    return {
        "hook": "fade",
        "context": "smoothleft",
        "escalation": "smoothleft",
        "evidence": "cut",
        "twist": "zoomin",
        "payoff": "circleopen",
        "closing": "fade",
    }.get(role, "cut")


def _default_voice_direction(emotion: str, intensity: int) -> str:
    base = {
        "curious": "santai, sedikit penasaran",
        "excited": "cepat dan bersemangat",
        "suspenseful": "pelan, seperti membocorkan rahasia",
        "surprised": "terkejut, lalu menekankan kata kunci",
        "conversational": "mengobrol santai, jelas",
        "comedic": "ringan, dengan timing punchline",
        "serious": "tenang dan tegas",
    }.get(emotion, "santai dan jelas")
    if intensity >= 4:
        base += ", lebih intens"
    return base


def _default_purpose(role: str) -> str:
    return {
        "hook": "menarik perhatian di detik pertama",
        "context": "memberi konteks minimum",
        "escalation": "menaikkan rasa penasaran",
        "evidence": "menjelaskan fakta utama",
        "twist": "memberi kejutan yang tetap berdasar fakta",
        "payoff": "membayar janji di awal",
        "closing": "menutup loop dan mendorong replay",
    }.get(role, "mendukung cerita")


def _fallback_visual_seed(narration: str, topic: str) -> str:
    return f"concrete cinematic illustration of {topic}, visual metaphor for: {narration[:80]}"


def _first_short(text: str, limit: int = 40) -> str:
    words = text.split()
    return " ".join(words[:5])[:limit]


def _render_facts(facts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, fact in enumerate(facts, start=1):
        confidence = fact.get("confidence", "unverified")
        verification = fact.get("verification", "no_source")
        lines.append(f"- F{index} [confidence={confidence}; verification={verification}] {fact.get('claim', '')}")
        if fact.get("quote"):
            lines.append(f"  detail: {fact['quote']}")
        if fact.get("source_title") or fact.get("source_url"):
            lines.append(f"  sumber: {fact.get('source_title') or fact.get('source_url')}")
    return "\n".join(lines)


def _fact_sort_key(ref: str) -> tuple[int, str]:
    try:
        return int(ref.lstrip("F")), ref
    except ValueError:
        return 999, ref


def _short_beat(narration: str) -> str:
    return " ".join(narration.split()[:8])


def _normalized_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _utcnow() -> str:
    from ..models import utcnow_iso

    return utcnow_iso()
