#!/usr/bin/env python3
"""
Daily Watch feed updater for Flatball.io.

Finds new uploads from trusted channels via the YouTube Data API v3, filters them
with the rules in config.json, and merges them into videos.json.

Embeds only: this script stores links and metadata. It never downloads or re-hosts
video or thumbnails.

Quota: ~2 units per channel per run (playlistItems.list + videos.list), plus a
one-time 1 unit per channel to resolve handles. The free daily quota is 10,000.

Usage:
  YOUTUBE_API_KEY=... python scripts/update_feed.py            # update videos.json
  YOUTUBE_API_KEY=... python scripts/update_feed.py --dry-run  # print, don't write
Standard library only (Python 3.9+).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
API = "https://www.googleapis.com/youtube/v3/"
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
VALID_CATEGORIES = {"play", "highlight", "strategy"}
VALID_PLATFORMS = {"youtube", "instagram"}
# Settings a channel entry in config.json may override.
CHANNEL_OVERRIDES = ("maxAgeDays", "minDurationSeconds", "maxDurationSeconds",
                     "maxDurationSecondsByCategory", "excludeKeywords")


def rules_for_channel(config: dict, channel: dict) -> dict:
    """Global rules with the channel's overrides applied."""
    rules = dict(config)
    rules.update({key: channel[key] for key in CHANNEL_OVERRIDES if key in channel})
    if channel.get("requireKeywords") is False:  # e.g. ultimate-only channels posting in Hindi
        rules["requireAnyKeyword"] = []
    return rules


# ---------------------------------------------------------------- API access

class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get(self, endpoint: str, **params) -> dict:
        params["key"] = self.api_key
        url = API + endpoint + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)

    def uploads_playlist(self, channel: dict) -> tuple[str, str, str | None] | None:
        """Returns (uploads playlist id, channel title, the channel's picture)."""
        params = {"part": "contentDetails,snippet"}
        if channel.get("channelId"):
            params["id"] = channel["channelId"]
        elif channel.get("handle"):
            params["forHandle"] = channel["handle"]
        elif channel.get("username"):
            params["forUsername"] = channel["username"]
        else:
            return None
        items = self.get("channels", **params).get("items", [])
        if not items:
            return None
        item = items[0]
        thumbnails = item["snippet"].get("thumbnails", {})
        picture = (thumbnails.get("medium") or thumbnails.get("default") or {}).get("url")
        return item["contentDetails"]["relatedPlaylists"]["uploads"], item["snippet"]["title"], picture

    def recent_upload_ids(self, playlist_id: str, limit: int = 15) -> list[str]:
        data = self.get("playlistItems", part="contentDetails", playlistId=playlist_id, maxResults=limit)
        return [i["contentDetails"]["videoId"] for i in data.get("items", [])]

    def video_details(self, video_ids: list[str]) -> list[dict]:
        if not video_ids:
            return []
        data = self.get("videos", part="snippet,contentDetails,status", id=",".join(video_ids[:50]))
        return data.get("items", [])

    def video_durations(self, video_ids: list[str]) -> dict[str, int]:
        """Length in seconds for each id YouTube still has: 1 quota unit per
        50 ids."""
        lengths: dict[str, int] = {}
        for start in range(0, len(video_ids), 50):
            data = self.get("videos", part="contentDetails", id=",".join(video_ids[start:start + 50]))
            for item in data.get("items", []):
                seconds = parse_iso_duration(item.get("contentDetails", {}).get("duration", ""))
                if seconds:
                    lengths[item["id"]] = seconds
        return lengths

    def channel_titles(self, video_ids: list[str]) -> dict[str, str]:
        """The channel each id was uploaded by: 1 quota unit per 50 ids. Only
        asked for videos added by hand without a creator."""
        titles: dict[str, str] = {}
        for start in range(0, len(video_ids), 50):
            data = self.get("videos", part="snippet", id=",".join(video_ids[start:start + 50]))
            for item in data.get("items", []):
                title = item.get("snippet", {}).get("channelTitle")
                if title:
                    titles[item["id"]] = title
        return titles


# ---------------------------------------------------------------- pure helpers

def parse_iso_duration(value: str) -> int | None:
    """'PT1H2M3S' -> 3723."""
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def youtube_id_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    candidate = None
    if host.endswith("youtu.be") and parts:
        candidate = parts[0]
    elif "youtube.com" in host or "youtube-nocookie.com" in host:
        if parts[:1] == ["watch"]:
            candidate = urllib.parse.parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in {"embed", "shorts", "live", "v"}:
            candidate = parts[1]
    return candidate if candidate and YOUTUBE_ID.match(candidate) else None


def slugify(text: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "video"


def clean_title(title: str) -> str:
    title = re.sub(r"#\w+", "", title)                      # hashtags
    title = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", title)  # emoji
    title = re.sub(r"\s*\|\s*", " | ", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -|")
    return title


def short_description(text: str, max_length: int = 160) -> str | None:
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"#\w+", "", text)
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 20:
            sentence = re.split(r"(?<=[.!?])\s", line)[0]
            if len(sentence) > max_length:
                sentence = sentence[: max_length - 1].rsplit(" ", 1)[0] + "…"
            return sentence
    return None


def contains_any(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", text) for k in keywords)


def categorize(title: str, description: str, config: dict, default: str) -> str:
    rules = config.get("categoryKeywords", {})
    # The title decides first; the description only when the title says
    # nothing. Descriptions are long and generic, so "Rookie of the Year
    # Highlights" was filed as strategy for a coaching word further down.
    # Order matters: a tutorial is strategy even if it mentions layouts, and a
    # "Top 10 layouts" compilation is a highlight rather than a single play.
    for text in (title, description):
        for category in ("strategy", "highlight", "play"):
            if contains_any(text, rules.get(category, [])):
                return category
    return default if default in VALID_CATEGORIES else "highlight"


def max_duration_for(config: dict, category: str | None) -> int:
    """Strategy earns a longer ceiling than the rest: film study and breakdowns
    routinely run past the 20 minutes that suits a highlight reel, and the old
    single cap silently rejected exactly the category the feed was short of."""
    default = config.get("maxDurationSeconds", 1200)
    if category is None:
        # Pre-categorisation: be permissive, or a long strategy video would be
        # thrown out before anyone asked what it was.
        by_category = config.get("maxDurationSecondsByCategory", {}).values()
        return max([default, *by_category]) if by_category else default
    return config.get("maxDurationSecondsByCategory", {}).get(category, default)


def reject_reason(item: dict, config: dict, now: dt.datetime, known_ids: set[str],
                  category: str | None = None) -> str | None:
    video_id = item["id"]
    snippet, details, status = item.get("snippet", {}), item.get("contentDetails", {}), item.get("status", {})
    title = snippet.get("title", "")
    text = f"{title} {snippet.get('description', '')}"

    if video_id in known_ids:
        return "already in feed"
    if video_id in set(config.get("blockedVideoIds", [])):
        return "blocked"
    if not status.get("embeddable", False):
        return "embedding disabled"
    if status.get("privacyStatus") != "public":
        return "not public"
    if snippet.get("liveBroadcastContent", "none") != "none":
        return "live or upcoming"
    published = snippet.get("publishedAt")
    if published:
        age = now - dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
        if age.days > config.get("maxAgeDays", 14):
            return "too old"
    duration = parse_iso_duration(details.get("duration", ""))
    if duration is None:
        return "unknown duration"
    if duration < config.get("minDurationSeconds", 20):
        return "too short"
    ceiling = max_duration_for(config, category)
    if duration > ceiling:
        return f"too long (over {ceiling}s for {category or 'any category'})"
    # Titles only: channels repeat the same promo text (podcast, merch,
    # tickets) under every upload, which turned away good videos wholesale.
    if contains_any(title, config.get("excludeKeywords", [])):
        return "excluded keyword"
    required = config.get("requireAnyKeyword", [])
    if required and not contains_any(text, required):
        return "not about ultimate"
    return None


def is_picture_url(url) -> bool:
    """Only YouTube's own picture servers: the app loads these directly."""
    return isinstance(url, str) and re.match(r"^https://yt\d*\.(ggpht|googleusercontent)\.com/", url) is not None


def creator_name(config: dict, channel_title: str, channel: dict | None = None) -> str:
    """The name players see. A channel's own `creator` wins; otherwise
    `creatorNames` tidies a YouTube title ("Ultiworld - Ultimate Frisbee" ->
    "Ultiworld") and keeps one name across a channel's renames."""
    if channel and channel.get("creator"):
        return channel["creator"]
    return config.get("creatorNames", {}).get(channel_title, channel_title)


def to_feed_entry(item: dict, channel_title: str, category: str, today: str, taken_ids: set[str]) -> dict:
    snippet = item["snippet"]
    title = clean_title(snippet.get("title", "")) or "Untitled video"
    base = slugify(title)
    slug, n = base, 2
    while slug in taken_ids:
        slug, n = f"{base}-{n}", n + 1
    entry = {
        "id": slug,
        "title": title,
        "embedURL": f"https://www.youtube.com/watch?v={item['id']}",
        "platform": "youtube",
        "category": category,
        "durationSeconds": parse_iso_duration(item["contentDetails"]["duration"]),
        "dateAdded": today,
        "description": short_description(snippet.get("description", "")),
        "creator": channel_title,
    }
    return {k: v for k, v in entry.items() if v is not None or k == "durationSeconds"}


def trim_feed(videos: list[dict], max_size: int, protect_since: str,
              min_per_category: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Keep newest. Never drop an entry with keep=true, one added on or after
    protect_since, or one whose category is already down to its floor.

    The basics (`basics`: the video's place on that shelf, from 1) are a shelf of their own: they are never
    dropped and never count towards the size or the floors, so adding one
    never pushes a new video out.

    The floors are what stop the feed drifting to whatever the busiest channels
    publish. Highlight channels upload most days and coaching channels upload
    most weeks, so without a floor the oldest-first trim grinds strategy out of
    the feed over a couple of months, however well the per-run quotas worked.
    """
    basics = [v for v in videos if v.get("basics")]
    ordered = sorted((v for v in videos if not v.get("basics")),
                     key=lambda v: v.get("dateAdded", ""), reverse=True)
    floors = min_per_category or {}
    removed: list[dict] = []
    while len(ordered) > max_size:
        counts = collections.Counter(v.get("category") for v in ordered)
        for index in range(len(ordered) - 1, -1, -1):
            candidate = ordered[index]
            if candidate.get("keep"):
                continue
            if candidate.get("dateAdded", "") >= protect_since:
                continue
            category = candidate.get("category")
            if counts.get(category, 0) <= floors.get(category, 0):
                continue
            removed.append(ordered.pop(index))
            break
        else:
            break  # everything left is protected
    together = sorted(ordered + basics, key=lambda v: v.get("dateAdded", ""), reverse=True)
    return together, removed


def validate(feed: dict) -> list[str]:
    errors, seen = [], set()
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", feed.get("version", "")):
        errors.append("version must be yyyy.MM.dd")
    for name, entry in feed.get("creators", {}).items():
        if not is_picture_url((entry or {}).get("avatar")):
            errors.append(f"creator {name}: avatar must be a YouTube picture URL")
    for video in feed.get("videos", []):
        vid = video.get("id")
        if not vid or vid in seen:
            errors.append(f"missing or duplicate id: {vid}")
        seen.add(vid)
        for field in ("title", "embedURL", "platform", "category", "dateAdded"):
            if not video.get(field):
                errors.append(f"{vid}: missing {field}")
        if video.get("platform") not in VALID_PLATFORMS:
            errors.append(f"{vid}: bad platform")
        if video.get("category") not in VALID_CATEGORIES:
            errors.append(f"{vid}: bad category")
        if video.get("platform") == "youtube" and not youtube_id_from_url(video.get("embedURL", "")):
            errors.append(f"{vid}: embedURL is not a YouTube video link")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", video.get("dateAdded", "")):
            errors.append(f"{vid}: dateAdded must be yyyy-MM-dd")
        basics = video.get("basics")
        if basics is not None and (isinstance(basics, bool) or not isinstance(basics, int) or basics < 1):
            errors.append(f"{vid}: basics must be a place on the shelf, from 1")
    return errors


# ---------------------------------------------------------------- main flow

def run(client, config: dict, feed: dict, now: dt.datetime) -> tuple[dict, list[dict], list[dict]]:
    local_now = now.astimezone(ZoneInfo(config.get("timezone", "UTC")))
    today = local_now.strftime("%Y-%m-%d")
    protect_since = (local_now - dt.timedelta(days=config.get("protectRecentDays", 7))).strftime("%Y-%m-%d")

    videos = [v for v in feed.get("videos", [])
              if youtube_id_from_url(v.get("embedURL", "")) not in set(config.get("blockedVideoIds", []))]
    known_youtube_ids = {youtube_id_from_url(v["embedURL"]) for v in videos if v.get("platform") == "youtube"}

    # Videos added by hand, or before lengths were recorded, have none; the
    # app shows a length on every card that has one. Looked up once, then kept.
    missing = {youtube_id_from_url(v["embedURL"]): v for v in videos
               if v.get("platform") == "youtube" and not v.get("durationSeconds")}
    missing.pop(None, None)
    filled = 0
    if missing and hasattr(client, "video_durations"):
        try:
            for video_id, seconds in client.video_durations(sorted(missing)).items():
                if video_id in missing:
                    missing[video_id]["durationSeconds"] = seconds
                    filled += 1
                    print(f"  length {seconds}s for {missing[video_id].get('title', '')[:60]!r}")
        except Exception as error:  # lengths are a nicety; never stop the run
            print(f"! video lengths: {error}", file=sys.stderr)

    # Creators: a tidy name for every video, including ones added by hand
    # without one and ones whose channel has since been renamed.
    renamed = 0
    nameless = {youtube_id_from_url(v["embedURL"]): v for v in videos
                if v.get("platform") == "youtube" and not v.get("creator")}
    nameless.pop(None, None)
    if nameless and hasattr(client, "channel_titles"):
        try:
            for video_id, title in client.channel_titles(sorted(nameless)).items():
                if video_id in nameless:
                    nameless[video_id]["creator"] = title
                    renamed += 1
        except Exception as error:  # a nicety too
            print(f"! creators: {error}", file=sys.stderr)
    for v in videos:
        if v.get("creator"):
            tidy = creator_name(config, v["creator"])
            if tidy != v["creator"]:
                v["creator"] = tidy
                renamed += 1
    taken_slugs = {v["id"] for v in videos}

    # Each creator's picture, from YouTube's own image servers, for the
    # app's row of creators. Kept from earlier runs for channels not asked
    # about this time; dropped once a creator has no video left.
    pictures: dict[str, str] = {name: entry["avatar"] for name, entry in feed.get("creators", {}).items()
                                if is_picture_url(entry.get("avatar"))}
    candidates: list[tuple[str, dict, str, str]] = []  # (publishedAt, item, channel title, default category)
    for channel in config.get("channels", []):
        label = channel.get("name") or channel.get("handle") or channel.get("username")
        try:
            resolved = client.uploads_playlist(channel)
            if not resolved:
                print(f"! channel not found: {label}", file=sys.stderr)
                continue
            playlist_id, channel_title, *rest = resolved
            channel_title = creator_name(config, channel_title, channel)
            if rest and is_picture_url(rest[0]):
                pictures[channel_title] = rest[0]
            items = client.video_details(client.recent_upload_ids(playlist_id))
        except Exception as error:  # one bad channel shouldn't stop the run
            print(f"! {label}: {error}", file=sys.stderr)
            continue

        rules = rules_for_channel(config, channel)
        accepted = 0
        for item in sorted(items, key=lambda i: i["snippet"].get("publishedAt", ""), reverse=True):
            snippet = item["snippet"]
            # Categorise FIRST. The category decides the duration ceiling and
            # which per-run quota the video competes for, so deciding it after
            # the cap -- as this used to -- let the busiest channels take every
            # slot before anyone asked what the videos were.
            category = categorize(snippet.get("title", ""), snippet.get("description", ""),
                                  config, channel.get("defaultCategory", "highlight"))
            reason = reject_reason(item, rules, now, known_youtube_ids, category)
            if reason:
                print(f"  skip [{label}] {snippet.get('title', '')[:60]!r}: {reason}")
                continue
            if accepted >= config.get("maxNewPerChannelPerRun", 1):
                break
            candidates.append((snippet.get("publishedAt", ""), item, channel_title, category))
            accepted += 1

    # Reserved slots per category. Without these the fill below is purely
    # newest-first, which is why the feed ran 7 highlights to 3 strategy.
    quotas = config.get("maxNewPerCategoryPerRun", {})
    taken_per_category: collections.Counter = collections.Counter()
    # Categories that only refresh every so many days (strategy weekly): one
    # batch, then nothing until the gap has passed since the last addition.
    last_added: dict[str, str] = {}
    for v in videos:
        if v.get("basics"):  # a basic added by hand starts no batch
            continue
        if v.get("dateAdded", "") > last_added.get(v.get("category"), ""):
            last_added[v["category"]] = v["dateAdded"]
    resting: dict[str, str] = {}
    for category, days in config.get("categoryEveryDays", {}).items():
        if category in last_added:
            next_day = (dt.date.fromisoformat(last_added[category]) + dt.timedelta(days=days)).isoformat()
            if today < next_day:
                resting[category] = next_day
    added: list[dict] = []
    for _, item, channel_title, category in sorted(candidates, key=lambda c: c[0], reverse=True):
        if len(added) >= config.get("maxNewPerRun", 3):
            break
        if category in resting:
            print(f"  hold [{category}] {item['snippet'].get('title', '')[:60]!r}: "
                  f"{category} next updates {resting[category]}")
            continue
        quota = quotas.get(category)
        if quota is not None and taken_per_category[category] >= quota:
            print(f"  hold [{category}] {item['snippet'].get('title', '')[:60]!r}: "
                  f"{category} is full for this run ({quota})")
            continue
        entry = to_feed_entry(item, channel_title, category, today, taken_slugs)
        taken_slugs.add(entry["id"])
        known_youtube_ids.add(item["id"])
        taken_per_category[category] += 1
        added.append(entry)

    videos, removed = trim_feed(videos + added, config.get("maxFeedSize", 50), protect_since,
                                config.get("minPerCategory"))
    names = {v.get("creator") for v in videos}
    creators = {name: {"avatar": url} for name, url in sorted(pictures.items()) if name in names}
    changed = bool(added or removed or filled or renamed or len(videos) != len(feed.get("videos", []))
                   or creators != feed.get("creators", {}))
    new_feed = {
        "version": local_now.strftime("%Y.%m.%d") if changed else feed.get("version", local_now.strftime("%Y.%m.%d")),
        "creators": creators,
        "videos": videos,
    }
    return new_feed, added, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the result without writing videos.json")
    parser.add_argument("--feed", default=str(ROOT / "videos.json"))
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    args = parser.parse_args()

    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        print("YOUTUBE_API_KEY is not set", file=sys.stderr)
        return 2

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    feed_path = Path(args.feed)
    feed = json.loads(feed_path.read_text(encoding="utf-8"))

    new_feed, added, removed = run(YouTubeClient(api_key), config, feed, dt.datetime.now(dt.timezone.utc))

    errors = validate(new_feed)
    if errors:
        print("Validation failed; videos.json not written:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1

    for video in added:
        print(f"+ [{video['category']}] {video['title']}  {video['embedURL']}")
    for video in removed:
        print(f"- {video['title']}")
    if new_feed["version"] == feed.get("version") and not added and not removed:
        print("No changes.")
        return 0

    output = json.dumps(new_feed, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(output)
    else:
        feed_path.write_text(output, encoding="utf-8")
        print(f"Wrote {feed_path.name}: {len(new_feed['videos'])} videos, version {new_feed['version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
