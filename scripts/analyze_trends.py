"""Read-only YouTube trends for Indonesia: mostPopular charts + niche keyword search.

Data only; this script never uploads, schedules, or modifies anything.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

from shortsgen.config import load_config
from shortsgen.youtube.auth import load_credentials

DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(value: str) -> int:
    match = DURATION_RE.fullmatch(value or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(part) if part else 0 for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def video_rows(youtube, ids: list[str]) -> list[dict]:
    rows: list[dict] = []
    for start in range(0, len(ids), 50):
        chunk = ids[start : start + 50]
        response = youtube.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(chunk)
        ).execute()
        for item in response.get("items", []):
            snippet = item["snippet"]
            stats = item.get("statistics") or {}
            rows.append({
                "video_id": item["id"],
                "title": snippet["title"],
                "channel": snippet["channelTitle"],
                "category_id": snippet.get("categoryId"),
                "published_at": snippet["publishedAt"],
                "duration_s": parse_duration((item.get("contentDetails") or {}).get("duration", "")),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "url": f"https://youtu.be/{item['id']}",
            })
    return rows


def chart_rows(youtube, region: str, category: str | None = None) -> list[dict]:
    params = {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": region,
        "maxResults": 50,
    }
    if category:
        params["videoCategoryId"] = category
    response = youtube.videos().list(**params).execute()
    rows = []
    for item in response.get("items", []):
        snippet = item["snippet"]
        stats = item.get("statistics") or {}
        rows.append({
            "video_id": item["id"],
            "title": snippet["title"],
            "channel": snippet["channelTitle"],
            "category_id": snippet.get("categoryId"),
            "published_at": snippet["publishedAt"],
            "duration_s": parse_duration((item.get("contentDetails") or {}).get("duration", "")),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "url": f"https://youtu.be/{item['id']}",
        })
    return rows


def niche_search(youtube, query: str, published_after: str, max_results: int = 25) -> list[dict]:
    response = youtube.search().list(
        part="snippet",
        q=query,
        regionCode="ID",
        relevanceLanguage="id",
        order="viewCount",
        publishedAfter=published_after,
        type="video",
        videoDuration="short",
        maxResults=max_results,
    ).execute()
    ids = [item["id"]["videoId"] for item in response.get("items", []) if item.get("id", {}).get("videoId")]
    return video_rows(youtube, ids)


def main() -> None:
    config = load_config()
    credentials = load_credentials(
        config.youtube_client_secrets_file, config.youtube_token_file, interactive=False
    )
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    now = datetime.now(timezone.utc)
    published_after = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload: dict = {
        "generated_at": now.isoformat(),
        "region": "ID",
        "source": "YouTube Data API v3, read-only (chart=mostPopular + search order=viewCount, 14 hari terakhir)",
        "charts": {},
        "niche_searches": {},
    }

    rows = chart_rows(youtube, "ID")
    category_counts: dict[str, int] = {}
    for row in rows:
        key = str(row["category_id"])
        category_counts[key] = category_counts.get(key, 0) + 1
    payload["charts"]["most_popular_all"] = {
        "category_filter": None,
        "note": "videoCategoryId filter tidak tersedia untuk region ID (HTTP 404); kategori dihitung dari hasil.",
        "category_counts": category_counts,
        "count": len(rows),
        "shorts_under_90s": len([r for r in rows if r["duration_s"] <= 90]),
        "top_shorts": sorted(
            [r for r in rows if r["duration_s"] <= 90], key=lambda r: r["views"], reverse=True
        )[:20],
        "all": rows,
    }

    for query in ["kenapa bisa", "fakta unik", "sehari-hari fakta"]:
        rows = niche_search(youtube, query, published_after)
        payload["niche_searches"][query] = {
            "published_after": published_after,
            "count": len(rows),
            "top": sorted(rows, key=lambda r: r["views"], reverse=True)[:20],
        }

    out = config.data_dir / "reports" / f"youtube_trends_{now.date().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    digest = {
        "saved": str(out),
        "category_counts": payload["charts"]["most_popular_all"]["category_counts"],
        "most_popular_all_shorts": [
            {"title": r["title"], "views": r["views"], "channel": r["channel"], "category_id": r["category_id"]}
            for r in payload["charts"]["most_popular_all"]["top_shorts"][:10]
        ],
        "niche_top": {
            q: [
                {"title": r["title"], "views": r["views"], "channel": r["channel"], "duration_s": r["duration_s"]}
                for r in data["top"][:8]
            ]
            for q, data in payload["niche_searches"].items()
        },
    }
    print(json.dumps(digest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
