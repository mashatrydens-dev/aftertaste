"""
aftertaste — daily music post bot
Reads database from Google Sheets (published CSV), asks Claude to write a post
in the channel's voice, posts to Telegram.
"""
import csv
import io
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import anthropic


# ──────────────────────────────────────────────────────────────────
# CONFIG — env vars set in GitHub Actions secrets
# ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]   # channel ID or your personal chat ID
TRACKS_CSV_URL = os.environ["TRACKS_CSV_URL"]       # public CSV link from Google Sheets (Треки sheet)
ARTISTS_CSV_URL = os.environ["ARTISTS_CSV_URL"]     # public CSV link (Артисты sheet)

CLAUDE_MODEL = "claude-opus-4-7"  # high quality for short, important creative output
PUBLISHED_FILE = Path("published.json")
MANIFESTO_FILE = Path("manifesto.md")
RECENT_POSTS_FILE = Path("recent_posts.json")  # last 5 posts for rotation


# ──────────────────────────────────────────────────────────────────
# 1. READ DATABASE FROM GOOGLE SHEETS (published CSV)
# ──────────────────────────────────────────────────────────────────
def fetch_csv(url: str) -> list[dict]:
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader if any(v.strip() for v in row.values() if v)]


def load_tracks() -> list[dict]:
    rows = fetch_csv(TRACKS_CSV_URL)
    tracks = []
    for r in rows:
        # column names match the Google Sheet headers
        artist = (r.get("Артист") or "").strip()
        track = (r.get("Трек") or "").strip()
        url = (r.get("YouTube-ссылка") or "").strip()
        if not (artist and track and url.startswith("http")):
            continue
        tracks.append({
            "artist": artist,
            "track": track,
            "album": (r.get("Альбом") or "").strip(),
            "year": (r.get("Год") or "").strip(),
            "youtube_url": url,
            "mood": (r.get("Настроение/контекст") or "").strip(),
            "notes": (r.get("Что сказать о треке") or "").strip(),
        })
    return tracks


def load_artists() -> list[dict]:
    rows = fetch_csv(ARTISTS_CSV_URL)
    artists = []
    for r in rows:
        name = (r.get("Артист") or "").strip()
        if not name:
            continue
        artists.append({
            "name": name,
            "genre": (r.get("Жанр / сцена") or "").strip(),
            "era": (r.get("Эпоха") or "").strip(),
            "love": (r.get("За что любишь (← заполни сама)") or
                     r.get("За что любишь") or "").strip(),
        })
    return artists


# ──────────────────────────────────────────────────────────────────
# 2. PERSISTENT STATE — published tracks and recent posts
# ──────────────────────────────────────────────────────────────────
def load_published() -> list[dict]:
    if not PUBLISHED_FILE.exists():
        return []
    return json.loads(PUBLISHED_FILE.read_text(encoding="utf-8"))


def save_published(entries: list[dict]) -> None:
    PUBLISHED_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_recent_posts() -> list[dict]:
    if not RECENT_POSTS_FILE.exists():
        return []
    return json.loads(RECENT_POSTS_FILE.read_text(encoding="utf-8"))


def save_recent_posts(posts: list[dict]) -> None:
    # keep only the last 5
    RECENT_POSTS_FILE.write_text(
        json.dumps(posts[-5:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────
# 3. ASK CLAUDE — generate a post in channel voice
# ──────────────────────────────────────────────────────────────────
def build_user_prompt(tracks: list[dict], artists: list[dict],
                       published: list[dict], recent: list[dict]) -> str:
    published_keys = {f"{p['artist']} — {p['track']}" for p in published}
    available = [t for t in tracks if f"{t['artist']} — {t['track']}" not in published_keys]

    # if all tracks used, allow republishing the oldest
    if not available:
        available = tracks[:]

    tracks_block = "\n".join(
        f"- {t['artist']} — {t['track']} ({t['album']}, {t['year']}) "
        f"| YouTube: {t['youtube_url']} "
        f"| настроение: {t['mood']!r} "
        f"| заметки автора: {t['notes']!r}"
        for t in available
    )

    artists_block = "\n".join(
        f"- {a['name']} [{a['genre']}, {a['era']}]: {a['love']}"
        for a in artists
    )

    recent_block = (
        "\n".join(f"- {p['artist']} — {p['track']}: {p['post_text'][:200]}…"
                  for p in recent[-5:])
        if recent else "Постов ещё не было — это первый."
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""Сегодня {now}. Время публикации — ночь по варшавскому времени (22:00–00:00).

ДОСТУПНЫЕ ТРЕКИ (выбирай только из этого списка, всё с YouTube-ссылками):
{tracks_block}

КОНТЕКСТ ПРО АРТИСТОВ (используй для понимания вкуса автора, не пересказывай):
{artists_block}

ПОСЛЕДНИЕ 5 ПОСТОВ (для правила чередования сцен — НЕ повторяй конфигурацию героев):
{recent_block}

Выбери ОДИН трек на сегодня и напиши пост СТРОГО ПО МАНИФЕСТУ.
Верни ТОЛЬКО валидный JSON, без markdown-обёртки, без комментариев:

{{
  "artist": "...",
  "track": "...",
  "album": "...",
  "year": ...,
  "youtube_url": "...",
  "post_text": "СЦЕНА\\n\\nhttps://...\\n\\n_Артист — Трек · Альбом, год_"
}}
"""


def generate_post(tracks, artists, published, recent) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = MANIFESTO_FILE.read_text(encoding="utf-8")
    user_prompt = build_user_prompt(tracks, artists, published, recent)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # strip possible code fences if Claude wrapped JSON despite instructions
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    post = json.loads(raw)

    # validation
    required = {"artist", "track", "youtube_url", "post_text"}
    if not required.issubset(post):
        raise ValueError(f"Claude returned incomplete JSON: {post}")

    # safety check: youtube_url must exist in our DB
    valid_urls = {t["youtube_url"] for t in tracks}
    if post["youtube_url"] not in valid_urls:
        raise ValueError(
            f"Claude returned a URL not in the database: {post['youtube_url']}"
        )

    return post


# ──────────────────────────────────────────────────────────────────
# 4. POST TO TELEGRAM
# ──────────────────────────────────────────────────────────────────
def send_to_telegram(text: str) -> dict:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,  # YouTube preview ON
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result["result"]


# ──────────────────────────────────────────────────────────────────
# 5. MAIN
# ──────────────────────────────────────────────────────────────────
def main():
    print(f"[aftertaste] start: {datetime.now(timezone.utc).isoformat()}")

    print("[1/5] loading tracks and artists from Google Sheets…")
    tracks = load_tracks()
    artists = load_artists()
    print(f"      → {len(tracks)} tracks, {len(artists)} artists")

    print("[2/5] loading state…")
    published = load_published()
    recent = load_recent_posts()
    print(f"      → {len(published)} already published, "
          f"{len(recent)} recent posts in memory")

    print("[3/5] asking Claude to write a post…")
    post = generate_post(tracks, artists, published, recent)
    print(f"      → picked: {post['artist']} — {post['track']}")
    print(f"      → post preview:\n{post['post_text'][:300]}…")

    print("[4/5] sending to Telegram…")
    tg_result = send_to_telegram(post["post_text"])
    print(f"      → message_id: {tg_result.get('message_id')}")

    print("[5/5] updating state files…")
    published.append({
        "date": datetime.now(timezone.utc).isoformat(),
        "artist": post["artist"],
        "track": post["track"],
        "youtube_url": post["youtube_url"],
        "telegram_message_id": tg_result.get("message_id"),
    })
    recent.append({
        "artist": post["artist"],
        "track": post["track"],
        "post_text": post["post_text"],
    })
    save_published(published)
    save_recent_posts(recent)
    print("[aftertaste] done. ✨")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[aftertaste] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
