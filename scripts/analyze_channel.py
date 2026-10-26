"""Read-only channel analysis: uploads playlist -> per-video statistics."""
from __future__ import annotations

import json

from googleapiclient.discovery import build

from shortsgen.config import load_config
from shortsgen.youtube.auth import load_credentials


def main() -> None:
    config = load_config()
    credentials = load_credentials(
        config.youtube_client_secrets_file, config.youtube_token_file, interactive=False
    )
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    channels = youtube.channels().list(part="snippet,contentDetails,statistics", mine=True).execute()
    items = channels.get("items") or []
    if not items:
        raise SystemExit("No channel")
    channel = items[0]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]

    videos: list[dict] = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="snippet,contentDetails", playlistId=uploads, maxResults=50, pageToken=page_token
        ).execute()
        for item in response.get("items", []):
            videos.append({
                "video_id": item["contentDetails"]["videoId"],
                "title": item["snippet"]["title"],
                "published_at": item["contentDetails"].get("videoPublishedAt")
                or item["snippet"]["publishedAt"],
            })
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    stats: list[dict] = []
    for start in range(0, len(videos), 50):
        chunk = videos[start : start + 50]
        response = youtube.videos().list(
            part="snippet,status,statistics",
            id=",".join(v["video_id"] for v in chunk),
        ).execute()
        by_id = {item["id"]: item for item in response.get("items", [])}
        for video in chunk:
            item = by_id.get(video["video_id"], {})
            snippet = item.get("snippet") or {}
            status = item.get("status") or {}
            stat = item.get("statistics") or {}
            stats.append({
                **video,
                "channel_title": snippet.get("channelTitle"),
                "category_id": snippet.get("categoryId"),
                "privacy": status.get("privacyStatus"),
                "made_for_kids": status.get("madeForKids"),
                "views": int(stat.get("viewCount", 0)),
                "likes": int(stat.get("likeCount", 0)),
                "comments": int(stat.get("commentCount", 0)),
            })

    payload = {
        "channel": {
            "id": channel["id"],
            "title": channel["snippet"]["title"],
            "subscribers": channel["statistics"].get("subscriberCount"),
            "total_views": channel["statistics"].get("viewCount"),
            "video_count": channel["statistics"].get("videoCount"),
        },
        "videos": sorted(stats, key=lambda v: v["views"], reverse=True),
    }
    out = config.data_dir / "reports" / "channel_analysis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("saved:", out)


if __name__ == "__main__":
    main()
