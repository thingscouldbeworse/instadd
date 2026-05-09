#!/usr/bin/env python3
"""
Poll an RSS/Atom feed and add new item URLs to Instapaper (Simple API).

Environment:
  INSTAPAPER_USERNAME  Instapaper email or username (required unless --dry-run)
  INSTAPAPER_PASSWORD  Password if the account has one; omit or empty if none

Usage:
  python rss_to_instapaper.py   # uses feeds.txt next to this script if present, else built-in default
  python rss_to_instapaper.py --feeds-file /path/to/feeds.txt
  python rss_to_instapaper.py --feed https://example.com/atom.xml  # extra feed (repeat flag for more)
  python rss_to_instapaper.py --limit 5   # add at most 5 new articles (--max-add is the same flag)
  python rss_to_instapaper.py --dry-run  # parse feeds only, no Instapaper calls

Cron: use run-cron.sh (sources .env next to the script) so PATH and credentials work.
  crontab -e → 0 2 * * * /full/path/to/instadd/run-cron.sh >> /full/path/to/instadd/cron.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import feedparser
import requests

INSTAPAPER_ADD_URL = "https://www.instapaper.com/api/add"

# Used only when no feeds.txt exists next to this script and no --feeds-file / --feed given.
DEFAULT_FEED_URLS: tuple[str, ...] = ("https://theintercept.com/feed/",)


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        root = Path(base)
    else:
        root = Path.home() / ".local" / "state"
    d = root / "rss-to-instapaper"
    d.mkdir(parents=True, exist_ok=True)
    return d / "seen.json"


def load_seen(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    if isinstance(data, dict) and "seen" in data:
        return {str(x) for x in data["seen"]}
    return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"seen": sorted(seen)}, indent=0, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def item_fingerprint(entry: Any) -> str | None:
    guid = getattr(entry, "id", None) or getattr(entry, "guid", None)
    if guid:
        g = guid
        if hasattr(g, "value"):
            g = g.value
        s = str(g).strip()
        if s:
            return f"guid:{s}"
    link = getattr(entry, "link", None)
    if link:
        s = str(link).strip()
        if s:
            return f"link:{s}"
    return None


def item_url(entry: Any) -> str | None:
    link = getattr(entry, "link", None)
    if not link:
        return None
    s = str(link).strip()
    return s or None


def item_title(entry: Any) -> str | None:
    t = getattr(entry, "title", None)
    if not t:
        return None
    s = str(t).strip()
    return s or None


def add_to_instapaper(
    session: requests.Session,
    username: str,
    password: str,
    url: str,
    title: str | None,
) -> tuple[int, str]:
    """POST to Instapaper Simple API. Returns (status_code, response_text_snippet)."""
    data: dict[str, str] = {"url": url}
    if title:
        data["title"] = title
    resp = session.post(
        INSTAPAPER_ADD_URL,
        data=data,
        auth=(username, password),
        timeout=60,
    )
    text = (resp.text or "").strip()[:500]
    return resp.status_code, text


def load_feed_urls_from_file(path: Path) -> list[str]:
    """One URL per line; # starts a comment to end of line; blank lines skipped."""
    text = path.read_text(encoding="utf-8")
    urls: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if line:
            urls.append(line)
    return urls


def resolve_feed_urls(feeds_file: Path | None, extra_feeds: list[str] | None) -> tuple[list[str], str]:
    """
    Returns (urls, description of source for messages).
    """
    script_feeds_txt = script_dir() / "feeds.txt"
    extras = list(extra_feeds or [])

    if feeds_file is not None:
        path = feeds_file.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        from_file = load_feed_urls_from_file(path)
        if not from_file:
            raise ValueError(f"No feed URLs in file: {path}")
        merged = _dedupe_preserve_order(from_file + extras)
        return merged, str(path)

    if script_feeds_txt.is_file():
        from_file = load_feed_urls_from_file(script_feeds_txt)
        if not from_file:
            raise ValueError(f"No feed URLs in file: {script_feeds_txt}")
        merged = _dedupe_preserve_order(from_file + extras)
        return merged, str(script_feeds_txt)

    if extras:
        return _dedupe_preserve_order(extras), "(--feed only)"

    return list(DEFAULT_FEED_URLS), "built-in default"


def _dedupe_preserve_order(urls: list[str]) -> list[str]:
    seen_u: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = u.strip()
        if not u or u in seen_u:
            continue
        seen_u.add(u)
        out.append(u)
    return out


def collect_new_items(
    feed_urls: list[str],
    already_seen: set[str],
) -> list[tuple[str, str, str | None]]:
    """Scan all feeds; return (fingerprint, article_url, title) not yet seen (deduped within this run)."""
    pending_fp: set[str] = set()
    to_send: list[tuple[str, str, str | None]] = []

    for feed_url in feed_urls:
        parsed = feedparser.parse(feed_url)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            print(
                f"Feed parse warning for {feed_url!r}: "
                f"{getattr(parsed, 'bozo_exception', 'unknown')}",
                file=sys.stderr,
            )

        entries = list(parsed.entries or [])
        entries.reverse()

        for entry in entries:
            fp = item_fingerprint(entry)
            url = item_url(entry)
            if not fp or not url:
                continue
            if fp in already_seen or fp in pending_fp:
                continue
            pending_fp.add(fp)
            to_send.append((fp, url, item_title(entry)))

    return to_send


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="RSS/Atom → Instapaper")
    p.add_argument(
        "--feeds-file",
        type=Path,
        default=None,
        help="Text file with one feed URL per line (# comments allowed). "
        f"If omitted, uses {script_dir() / 'feeds.txt'} when that file exists, else built-in defaults.",
    )
    p.add_argument(
        "--feed",
        action="append",
        dest="extra_feeds",
        metavar="URL",
        help="Additional feed URL (repeat for multiple). Merged after URLs from the feeds file.",
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help=f"JSON file tracking seen items (default: {default_state_path()})",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse feed and print actions only; do not call Instapaper or update state",
    )
    p.add_argument(
        "--max-add",
        "--limit",
        type=int,
        default=0,
        dest="max_add",
        metavar="N",
        help="Add at most N new articles this run (0 = no limit; same as --limit)",
    )
    p.add_argument(
        "--sleep-seconds",
        type=float,
        default=1.0,
        help="Pause between Instapaper API calls",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    state_path = args.state_file or default_state_path()
    seen = load_seen(state_path) if not args.dry_run else set()

    user = os.environ.get("INSTAPAPER_USERNAME", "").strip()
    password = os.environ.get("INSTAPAPER_PASSWORD", "")

    if not args.dry_run and not user:
        print("INSTAPAPER_USERNAME is required (or use --dry-run).", file=sys.stderr)
        return 2

    try:
        feed_urls, source_label = resolve_feed_urls(args.feeds_file, args.extra_feeds)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"Feeds ({len(feed_urls)}): from {source_label}")

    to_send = collect_new_items(feed_urls, seen)

    if args.max_add > 0:
        to_send = to_send[: args.max_add]

    if not to_send:
        print("No new items.")
        return 0

    session = requests.Session()
    added = 0
    for fp, url, title in to_send:
        if args.dry_run:
            print(f"would add: {url}")
            if title:
                print(f"  title: {title}")
            continue

        code, body = add_to_instapaper(session, user, password, url, title)
        if code == 201:
            print(f"added ({code}): {url}")
            seen.add(fp)
            added += 1
            save_seen(state_path, seen)
        elif code == 400:
            print(
                f"Instapaper 400 (bad request or rate limit): {url}\n  body: {body}",
                file=sys.stderr,
            )
            return 1
        elif code == 403:
            print("Instapaper 403: check INSTAPAPER_USERNAME / INSTAPAPER_PASSWORD.", file=sys.stderr)
            return 1
        else:
            print(f"Instapaper {code} for {url}: {body}", file=sys.stderr)
            return 1

        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    if args.dry_run:
        print(f"--dry-run: {len(to_send)} new item(s) would be sent.")
    else:
        print(f"Done. Added {added} item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
