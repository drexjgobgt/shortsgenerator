"""Gerbang relevansi visual: heuristik teks + pemeriksaan vision opsional.

Prinsip kejujuran:
- Skor heuristik hanya mengukur kecocokan kata konkret antara narasi dan rencana
  visual. Ini BUKAN penilaian estetika objektif.
- Pemeriksaan vision memakai model multimodal bila diaktifkan; bila gagal, status
  dicatat `vision_failed` dan heuristik tetap dipakai (tidak menyamar berhasil).
- Temuan di bawah ambang tidak pernah disembunyikan: aset ditandai
  `needs_human_review` dan alasannya ditulis di relevance.json.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Optional

from ..util.jsonparse import JsonParseError, extract_json_object

# Kata yang hampir selalu muncul di prompt visual dan tidak membuktikan relevansi.
GENERIC_VISUAL_TOKENS = {
    "cinematic", "scene", "shot", "image", "visual", "subject", "action", "location",
    "composition", "camera", "lighting", "palette", "style", "stylized", "realism",
    "detailed", "detail", "background", "foreground", "depth", "field", "lens",
    "volumetric", "light", "soft", "warm", "cool", "color", "colors", "film",
    "grain", "texture", "render", "rendered", "animation", "animated", "still",
    "documentary", "illustration", "concept", "abstract", "beautiful", "dramatic",
    "atmospheric", "realistic", "believable", "high", "quality", "vertical",
    "composition", "negative", "prompt", "no", "without", "same", "consistent",
}

STOPWORDS_ID = {
    "yang", "dan", "di", "ke", "dari", "ini", "itu", "dengan", "untuk", "pada",
    "adalah", "akan", "bisa", "tidak", "bukan", "juga", "sangat", "lebih", "saat",
    "kita", "kamu", "mereka", "dia", "ia", "ada", "jadi", "tapi", "tetapi", "atau",
    "karena", "kalau", "jika", "sudah", "belum", "masih", "hanya", "saja", "punya",
    "memiliki", "seperti", "sebuah", "seorang", "para", "the", "and", "with", "for",
    "that", "this", "into", "from", "over", "under", "inside", "its", "their",
}

# Padanan istilah umum ID<->EN agar heuristik bisa membandingkan narasi Indonesia
# dengan rencana visual berbahasa Inggris. Ini kamus kecil, bukan penerjemah.
BILINGUAL_HINTS: dict[str, tuple[str, ...]] = {
    "gurita": ("octopus",), "jantung": ("heart", "hearts"), "darah": ("blood",),
    "ubur": ("jellyfish", "medusa"), "bayi": ("baby", "juvenile", "young"),
    "menua": ("aging", "age"), "tua": ("old", "aging"), "uzur": ("old", "aging"),
    "sel": ("cell", "cells"), "polip": ("polyp", "polyps"),
    "tentakel": ("tentacle", "tentacles"), "bel": ("bell",),
    "renang": ("swimming", "swim"), "predator": ("predator",),
    "luka": ("injured", "wound"), "lapar": ("hungry", "starvation"),
    "jaringan": ("tissue",), "berubah": ("morphing", "transforming", "change"),
    "kembali": ("back", "revert", "return"), "abadi": ("immortal", "eternal"),
    "cahaya": ("light",), "sinar": ("light", "sunlight", "rays"),
    "telur": ("egg", "eggs"), "koloni": ("colony",),
    "mikroskop": ("microscope", "microscopic"), "makhluk": ("creature",),
    "hewan": ("animal",), "bebek": ("duck",), "sepeda": ("bicycle", "bike"),
    "lucu": ("funny", "cute"), "topi": ("hat", "cap"), "topinya": ("hat", "cap"),
    "kura": ("turtle", "turtles"), "merah": ("red",),
    "kuning": ("yellow",), "mi": ("noodle", "noodles"), "mie": ("noodle", "noodles"), "ayam": ("chicken",),
    "genangan": ("puddle",), "loncat": ("jump", "jumping"), "basah": ("wet",),
    "ketawa": ("laughing", "laugh"), "jalan": ("road", "street"),
    "kota": ("town", "city"), "enak": ("yummy", "delicious"),
    "selamat": ("safe",), "teman": ("friend", "friends"),
    "kring": ("bell",), "ontel": ("bicycle", "bike"),
    "biru": ("blue",), "laut": ("ocean", "sea"), "dasar": ("seafloor", "bottom"),
    "berenang": ("swimming", "swim"), "insang": ("gills",), "tembaga": ("copper",),
    "oksigen": ("oxygen",), "mengikat": ("bind", "binding"), "dingin": ("cold",),
    "sedikit": ("less", "little"), "molekul": ("molecule", "molecules"),
    "memompa": ("pumping", "pump"), "dua": ("two",), "tiga": ("three",),
    "satu": ("one",), "kecil": ("small", "little"), "utama": ("main", "central"),
    "sistemik": ("systemic",), "brankial": ("branchial",), "tubuh": ("body",),
    "seluruh": ("whole", "entire"), "berhenti": ("pauses", "stops", "stop"),
    "cepat": ("fast", "quick"), "kencang": ("fast",), "malah": ("instead",),
    "tekanan": ("pressure",), "mantel": ("mantle",), "merayap": ("crawling", "crawl"),
    "suka": ("prefer",), "bebas": ("free",), "berarti": ("means",),
    "istirahat": ("resting", "rest"), "batu": ("rock",), "karang": ("coral", "reef"),
    "pesawat": ("airplane", "aircraft", "plane"), "sayap": ("wing", "wings"),
    "udara": ("air",), "terbang": ("flying", "flight"), "langit": ("sky",),
    "mars": ("mars",), "planet": ("planet",), "bintang": ("star", "stars"),
    "bulan": ("moon",), "matahari": ("sun",), "pelangi": ("rainbow",),
    "hujan": ("rain",), "awan": ("cloud", "clouds"), "petir": ("lightning",),
    "gunung": ("mountain",), "api": ("fire",), "es": ("ice",), "hutan": ("forest",),
    "lebah": ("bee",), "madu": ("honey",), "paus": ("whale",), "ikan": ("fish",),
    "burung": ("bird",), "kucing": ("cat",), "anjing": ("dog",),
    "otak": ("brain",), "mata": ("eye", "eyes", "tears", "tear"), "tidur": ("sleep",),
    "jari": ("finger", "fingers", "fingertip", "fingertips"), "air": ("water",),
    "keriput": ("wrinkle", "wrinkles", "wrinkled", "wrinkling"),
    "kerutan": ("wrinkle", "wrinkles", "wrinkling"), "saraf": ("nerve", "nerves", "nervous"),
    "pembuluh": ("vessel", "vessels"),
    "gelombang": ("wave", "waves"), "berdiri": ("standing",),
    "panas": ("hot", "heat", "warm", "heating"), "putar": ("turntable", "rotating", "rotation", "rotate", "spin"),
    "oven": ("oven",), "logam": ("metal",), "wadah": ("container", "containers"),
    "hantaran": ("conduction", "conduct"), "merata": ("even", "uniform", "uneven", "evenly"),
    "bawang": ("onion", "onions"), "menangis": ("cry", "crying", "tears"),
    "enzim": ("enzyme", "enzymes"), "dipotong": ("cut", "cutting", "sliced"),
    "potong": ("cut", "cutting", "sliced"), "gas": ("gas", "vapor", "vapour"),
    "uap": ("vapor", "vapors", "vapour", "vapours", "mist", "steam"),
    "uapnya": ("vapor", "vapors", "vapour", "vapours", "mist", "steam"),
    "kornea": ("cornea", "corneal"),
    "kelenjar": ("gland", "glands"), "belerang": ("sulfur", "sulphur"),
    "iritan": ("irritant",), "refleks": ("reflex",), "trik": ("trick", "tricks"),
    "penelitian": ("research", "study", "studies"),
    "nanas": ("pineapple", "pineapples"), "lidah": ("tongue", "tongues"),
    "perih": ("burn", "burning", "sting", "stinging", "tingling", "prickle"),
    "memotong": ("cut", "cutting"), "rantai": ("chain", "chains"),
    "duri": ("needle", "needles", "spike"), "kristal": ("crystal", "crystals"),
    "kalsium": ("calcium",), "oksalat": ("oxalate",),
    "menusuk": ("prick", "pricking", "puncture", "piercing"),
    "kaleng": ("canned", "can"), "pengawetan": ("canning", "preservation", "heat"),
    "mematikan": ("destroys", "kills", "denatures"), "racun": ("poison", "toxic"),
    "alergi": ("allergy", "allergic"), "pelindung": ("protective", "protection", "lining", "shield"),
    "mulut": ("mouth", "oral"), "mulutmu": ("mouth", "oral"),
    "lapisan": ("layer", "lining", "surface"), "gelatin": ("gelatin", "jello"),
    "mencerna": ("digest", "digesting", "digestion"), "kombinasi": ("combination", "combined", "mix"),
    "dihipotesiskan": ("hypothesis", "hypothesized", "hypotheses"), "hipotesis": ("hypothesis", "hypothesized", "hypotheses"),
    "peneliti": ("researcher", "researchers", "study"),
    "bayi": ("baby", "infant", "crib"), "ingatan": ("memory", "memories"),
    "ingat": ("remember", "recall", "memory"), "ingatanmu": ("memory", "memories"),
    "masa": ("period", "early"), "otak": ("brain", "brains"),
    "hippocampus": ("hippocampus", "hippocampal"), "matang": ("mature", "maturity", "developed"),
    "genteng": ("tile", "tiles", "roof"), "atap": ("roof", "rooftop"),
    "bocor": ("leak", "leaking", "leakage"), "retak": ("crack", "cracked", "cracks"),
    "celah": ("gap", "gaps", "joint", "joints"), "sambungan": ("joint", "joints", "connection", "seam"),
    "tanah": ("clay", "earth"), "liat": ("clay",), "pori": ("pore", "pores", "porous"),
    "meresap": ("absorb", "absorption", "seep"), "talang": ("gutter", "gutters", "drainage"),
    "mampet": ("clogged", "clog", "blocked"), "kemiringan": ("slope", "pitch"),
    "kabel": ("cable", "fiber", "fibre"), "fiber": ("fiber", "fibre", "optical"),
    "optik": ("optic", "optical"), "cahaya": ("light",), "kaca": ("glass",),
    "inti": ("core",), "ujung": ("end", "ends", "tip"), "lakban": ("tape", "adhesive"),
    "selotip": ("tape",), "fusi": ("fusion", "splice", "splicing"), "sambung": ("splice", "join", "connect"),
    "disambung": ("splice", "spliced", "joined"), "memantulkan": ("reflect", "reflection", "reflects"),
    "pantulan": ("reflection", "reflectance", "back"), "sinar": ("light", "beam"),
    "pantul": ("reflection", "reflect", "back"), "balik": ("back", "backward", "return"),
    "pengirim": ("transmitter", "sender", "source"), "buruk": ("bad", "poor", "imperfect"),
    "kamar": ("room",), "boks": ("crib", "cot", "bed"), "selimut": ("blanket",),
    "foto": ("photo", "photograph", "picture"), "pudar": ("faded", "fading"),
    "kaca": ("glass", "frosted"), "beku": ("frosted", "frozen", "glass"),
    "sepatu": ("shoe", "shoes"), "bayi": ("baby", "infant", "crib"),
    "membentuk": ("shape", "shaping", "form"), "membentukmu": ("shape", "shaping", "form"),
    "dipanggil": ("recall", "retrieve", "retrieval"), "jejak": ("trace", "traces"),
    "akses": ("access", "retrieval"), "pemicu": ("cue", "trigger", "reminder"),
    "tikus": ("rat", "rats", "rodent"), "usia": ("age", "years"),
    "penyimpan": ("storage", "store", "memory"), "kejadian": ("event", "events", "episodic"),
    "aksesnya": ("access", "retrieval"), "jejaknya": ("trace", "traces"),
    "hilang": ("lost", "missing", "gone"), "seluruh": ("whole", "entire", "all"),
    "pinggir": ("edge", "side", "rim"), "pinggirnya": ("edge", "side", "rim"),
    "jarak": ("distance", "spacing", "gap", "separated"),
    "tengahnya": ("middle", "center", "centre"), "tebal": ("thick",),
    "aduk": ("stir", "stirring", "mix", "mixing"), "naik": ("rise", "rising"),
    "menembus": ("penetrate", "penetration"),
    "sentimeter": ("centimeter", "centimeters", "centimetre", "inch", "inches"),
    "daya": ("power",), "rendah": ("low", "lower"), "sulit": ("hard", "harder", "difficult"),
    "senyawa": ("compound", "compounds"), "kedua": ("second",),
    "mengubahnya": ("becoming", "transforms", "transform", "changes", "change"),
    "menguap": ("volatile", "evaporate", "evaporation", "vapor", "vapors"),
    "membilas": ("rinse", "rinses", "rinsing", "wash", "washes", "flush", "flushes"),
    "membilasnya": ("rinse", "rinses", "rinsing", "wash", "washes", "flush", "flushes"),
    "mendinginkan": ("chilling", "chilled", "cooling", "refrigerator", "fridge", "refrigerate"),
    "mimpi": ("dream",), "kulit": ("skin",), "makanan": ("food",),
    "kopi": ("coffee",), "cokelat": ("chocolate",), "gula": ("sugar",),
    "garam": ("salt",), "komputer": ("computer",), "robot": ("robot",),
    "mesin": ("machine", "engine"), "listrik": ("electricity",),
    "chip": ("chip",), "kode": ("code",), "sejarah": ("history",),
    "perang": ("war",), "kerajaan": ("kingdom",), "kuno": ("ancient",),
    "mesir": ("egypt",), "romawi": ("rome", "roman"),
}


def _expand_bilingual(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for token in tokens:
        expanded.update(BILINGUAL_HINTS.get(token, ()))
    return expanded


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if len(token) > 2 and token not in STOPWORDS_ID
    }


def concrete_tokens(text: str) -> set[str]:
    """Token yang dianggap bukti isi (bukan kata generik visual)."""
    return _tokens(text) - GENERIC_VISUAL_TOKENS


def score_visual_plan(
    narration: str,
    visual_seed: str,
    *,
    subject: str = "",
    action: str = "",
    topic: str = "",
) -> dict[str, Any]:
    """Skor 0-10 kecocokan rencana visual dengan narasi (heuristik transparan).

    Komponen:
    - +5 bila ada >=1 token konkret yang sama antara narasi dan visual plan.
    - +2 per token konkret tambahan yang sama (maks +4).
    - +1 bila subjek konkret ada di visual_seed.
    - penalti -1 per kata generik yang mendominasi (maks -3).
    Topik ikut dihitung sebagai jangkar subjek (mis. "gurita" -> "octopus").
    """
    narration_tokens = _expand_bilingual(concrete_tokens(narration) | concrete_tokens(topic))
    plan_text = " ".join(part for part in (subject, action, visual_seed) if part)
    plan_tokens = _expand_bilingual(concrete_tokens(plan_text))
    shared = narration_tokens & plan_tokens
    score = 0.0
    reasons: list[str] = []
    if shared:
        score += 5
        score += min(4, 2 * (len(shared) - 1))
        reasons.append("token konkret sama: " + ", ".join(sorted(shared)[:6]))
    else:
        reasons.append("tidak ada token konkret yang sama antara narasi dan rencana visual")
    if subject and concrete_tokens(subject) & narration_tokens:
        score += 1
        reasons.append("subjek visual disebut di narasi")
    raw_plan_tokens = _tokens(plan_text)
    generic_count = len(raw_plan_tokens & GENERIC_VISUAL_TOKENS)
    if generic_count >= 3:
        penalty = min(3, generic_count - 2)
        score -= penalty
        reasons.append(f"penalti {penalty}: terlalu banyak kata generik visual")
    score = max(0.0, min(10.0, score))
    return {
        "score": round(score, 1),
        "method": "heuristic",
        "shared_tokens": sorted(shared)[:10],
        "generic_tokens": generic_count,
        "reasons": reasons,
        "needs_human_review": score < 4.0,
    }


def improve_prompt(prompt: str, narration: str, visual_seed: str) -> str:
    """Tambahkan jangkar konkret dari narasi agar regenerasi lebih relevan."""
    anchor = (visual_seed or narration).strip()
    if not anchor:
        return prompt
    addition = f" Must clearly depict: {anchor[:160]}."
    if addition.strip().lower() in prompt.lower():
        return prompt
    return (prompt.rstrip() + addition).strip()


VISION_PROMPT = """Nilai apakah GAMBAR ini benar-benar menggambarkan NARASI scene.

Narasi (Bahasa Indonesia): {narration}
Rencana visual: {visual_seed}
Gaya yang diharapkan: {style}

Jawab HANYA JSON valid:
{{"score": 0-10, "reason": "alasan singkat", "better_prompt": "prompt gambar bahasa Inggris yang lebih tepat bila skor < 7, selain itu kosong"}}

Aturan: skor 0 bila gambar tidak berhubungan; 5 bila hanya tema umumnya cocok; 8+ bila subjek dan aksi utama terlihat jelas. Jangan menilai estetika."""


def vision_check(
    llm: Any,
    image_path: Path,
    *,
    narration: str,
    visual_seed: str,
    style: str = "",
    model: Optional[str] = None,
) -> dict[str, Any]:
    """Pemeriksaan vision opsional. Melempar exception bila provider tidak mendukung."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(path)
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = VISION_PROMPT.format(
        narration=narration[:500],
        visual_seed=visual_seed[:300],
        style=style[:200],
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }
    ]
    kwargs: dict[str, Any] = {"temperature": 0.0, "max_tokens": 400}
    if model:
        kwargs["model"] = model
    result = llm.chat(messages, **kwargs)
    data = extract_json_object(result.text)
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        raise JsonParseError(f"skor vision tidak valid: {data.get('score')!r}")
    score = max(0.0, min(10.0, score))
    return {
        "score": round(score, 1),
        "method": "vision",
        "reason": str(data.get("reason") or "")[:300],
        "better_prompt": str(data.get("better_prompt") or "")[:500],
        "model": getattr(result, "model", model or ""),
        "needs_human_review": score < 7.0,
    }


def summarize_relevance(entries: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    """Ringkas hasil pemeriksaan relevansi untuk relevance.json + laporan evaluasi."""
    scored = [entry for entry in entries if entry.get("relevance")]
    below = [entry for entry in scored if float(entry["relevance"].get("score") or 0) < threshold]
    methods: dict[str, int] = {}
    for entry in scored:
        method = str((entry.get("relevance") or {}).get("method") or "unknown")
        methods[method] = methods.get(method, 0) + 1
    average = (
        round(sum(float(entry["relevance"]["score"]) for entry in scored) / len(scored), 1)
        if scored
        else None
    )
    return {
        "scenes_scored": len(scored),
        "average_score": average,
        "threshold": threshold,
        "below_threshold": [
            {"scene_index": entry.get("scene_index"), "score": entry["relevance"].get("score")}
            for entry in below
        ],
        "needs_human_review": sorted(
            {int(entry["scene_index"]) for entry in entries if (entry.get("relevance") or {}).get("needs_human_review")}
        ),
        "methods": methods,
        "entries": entries,
    }
