#!/usr/bin/env python3
"""
Poll an RSS/Atom feed and add new item URLs to Instapaper (Simple API),
and optionally prune stale unread bookmarks via Instapaper Full API (OAuth 1.0a).

Environment:
  INSTAPAPER_USERNAME     Instapaper email or username (required unless --dry-run)
  INSTAPAPER_PASSWORD     Password if the account has one; omit or empty if none
  CONSUMER_KEY            Instapaper OAuth Consumer Key (required for staleness pruning)
  CONSUMER_SECRET         Instapaper OAuth Consumer Secret (required for staleness pruning)

Usage:
  python rss_to_instapaper.py   # uses feeds.txt next to this script if present, else built-in default
  python rss_to_instapaper.py --feeds-file /path/to/feeds.txt
  python rss_to_instapaper.py --feed https://example.com/atom.xml  # extra feed (repeat flag for more)
  python rss_to_instapaper.py --limit 5   # N: per-run Instapaper cap (global this run)
  python rss_to_instapaper.py --dry-run  # parse feeds only, no Instapaper mutations
  python rss_to_instapaper.py --skip-prune # skip pruning step

feeds.txt format:
  URL [M] [staleness_days]
  - M: only consider first M entries from XML (usually M newest).
  - staleness_days: prune articles from this source older than this many days.

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
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlparse

import feedparser
import requests
from requests_oauthlib import OAuth1

INSTAPAPER_ADD_URL = "https://www.instapaper.com/api/add"
INSTAPAPER_OAUTH_TOKEN_URL = "https://www.instapaper.com/api/1/oauth/access_token"
INSTAPAPER_BOOKMARKS_LIST_URL = "https://www.instapaper.com/api/1/bookmarks/list"
INSTAPAPER_BOOKMARKS_DELETE_URL = "https://www.instapaper.com/api/1/bookmarks/delete"


class FeedSpec(NamedTuple):
    url: str
    depth_m: int | None = None
    max_age_days: int | None = None


# Used only when no feeds.txt exists next to this script and no --feeds-file / --feed given.
DEFAULT_FEED_SPECS: tuple[FeedSpec, ...] = (
    FeedSpec("https://theintercept.com/feed/", None, None),
)


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


def normalize_domain(url: str) -> str:
    parsed = urlparse(url)
    domain = (parsed.netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def bookmark_matches_feed(bookmark_url: str, feed_url: str) -> bool:
    """Check whether a bookmark URL belongs to the source of feed_url."""
    b_domain = normalize_domain(bookmark_url)
    f_domain = normalize_domain(feed_url)
    if not b_domain or not f_domain:
        return False
    if b_domain != f_domain:
        return False

    # Check path prefix if the feed URL is not root-level
    f_path = urlparse(feed_url).path.rstrip("/")
    for suffix in ("/feed", "/rss", "/atom.xml", "/feed.xml", "/index.xml", "/rss.xml"):
        if f_path.endswith(suffix):
            f_path = f_path[: -len(suffix)].rstrip("/")
            break
    if f_path and f_path != "":
        b_path = urlparse(bookmark_url).path
        if not b_path.startswith(f_path):
            return False

    return True


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


def get_instapaper_oauth_session(
    username: str,
    password: str,
    consumer_key: str,
    consumer_secret: str,
    timeout: float = 60.0,
) -> requests.Session:
    """Authenticate with Instapaper Full API via xAuth and return an authenticated requests.Session."""
    auth = OAuth1(client_key=consumer_key, client_secret=consumer_secret)
    payload = {
        "x_auth_username": username,
        "x_auth_password": password,
        "x_auth_mode": "client_auth",
    }
    resp = requests.post(INSTAPAPER_OAUTH_TOKEN_URL, auth=auth, data=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Instapaper OAuth failed ({resp.status_code}): {resp.text.strip()[:300]}"
        )
    tokens = parse_qs(resp.text)
    if "oauth_token" not in tokens or "oauth_token_secret" not in tokens:
        raise RuntimeError(f"Unexpected OAuth response from Instapaper: {resp.text.strip()[:300]}")

    oauth_token = tokens["oauth_token"][0]
    oauth_token_secret = tokens["oauth_token_secret"][0]

    user_auth = OAuth1(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=oauth_token,
        resource_owner_secret=oauth_token_secret,
    )
    session = requests.Session()
    session.auth = user_auth
    return session


def fetch_bookmarks(session: requests.Session, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch unread bookmarks from Instapaper Full API."""
    resp = session.post(INSTAPAPER_BOOKMARKS_LIST_URL, data={"limit": limit}, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch bookmarks ({resp.status_code}): {resp.text.strip()[:300]}"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Invalid JSON from Instapaper bookmarks list: {e}") from e

    bookmarks: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("type") == "bookmark":
                bookmarks.append(item)
    elif isinstance(data, dict):
        for item in data.get("bookmarks", []):
            if isinstance(item, dict):
                bookmarks.append(item)
    return bookmarks


def prune_stale_bookmarks(
    feed_specs: list[FeedSpec],
    username: str,
    password: str,
    consumer_key: str,
    consumer_secret: str,
    dry_run: bool = False,
    sleep_seconds: float = 1.0,
) -> int:
    """Find and delete bookmarks in Instapaper older than max_age_days for matching feeds."""
    prune_feeds = [f for f in feed_specs if f.max_age_days is not None]
    if not prune_feeds:
        return 0

    if not consumer_key or not consumer_secret:
        print(
            "Notice: Staleness pruning configured in feeds.txt, but CONSUMER_KEY and CONSUMER_SECRET "
            "are not set. Skipping pruning.",
            file=sys.stderr,
        )
        return 0

    if not username:
        print("Notice: INSTAPAPER_USERNAME not set; skipping pruning.", file=sys.stderr)
        return 0

    print(f"Checking staleness pruning for {len(prune_feeds)} feed(s)...")
    try:
        session = get_instapaper_oauth_session(username, password, consumer_key, consumer_secret)
        bookmarks = fetch_bookmarks(session)
    except Exception as e:
        print(f"Error during Instapaper pruning: {e}", file=sys.stderr)
        return 0

    now = time.time()
    deleted_count = 0
    deleted_ids: set[Any] = set()

    for feed in prune_feeds:
        max_days = feed.max_age_days
        assert max_days is not None
        cutoff_timestamp = now - (max_days * 86400)

        for bm in bookmarks:
            bm_id = bm.get("bookmark_id") or bm.get("id")
            bm_url = bm.get("url", "")
            bm_time = bm.get("time", 0)

            if not bm_id or not bm_url or bm_id in deleted_ids:
                continue

            if bookmark_matches_feed(bm_url, feed.url):
                if bm_time < cutoff_timestamp:
                    age_days = (now - bm_time) / 86400
                    if dry_run:
                        print(
                            f"would prune: {bm_url} (id={bm_id}, age={age_days:.1f}d > {max_days}d)"
                        )
                        deleted_count += 1
                        deleted_ids.add(bm_id)
                    else:
                        del_resp = session.post(
                            INSTAPAPER_BOOKMARKS_DELETE_URL,
                            data={"bookmark_id": bm_id},
                            timeout=60,
                        )
                        if del_resp.status_code == 200:
                            print(
                                f"pruned ({del_resp.status_code}): {bm_url} (age={age_days:.1f}d > {max_days}d)"
                            )
                            deleted_count += 1
                            deleted_ids.add(bm_id)
                        else:
                            print(
                                f"Failed to prune {bm_url} (id={bm_id}, status={del_resp.status_code}): "
                                f"{del_resp.text.strip()[:200]}",
                                file=sys.stderr,
                            )
                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)

    return deleted_count


def parse_feed_spec_line(line: str) -> FeedSpec:
    """
    One feed spec per line after comment stripping:
      URL
      URL <positive int M>
      URL <positive int M> <positive int staleness_days>
      URL - <positive int staleness_days>
      URL 0 <positive int staleness_days>
    """
    parts = line.split()
    if not parts:
        raise ValueError("empty feed line")

    # Check for 3+ parts: URL [M] [staleness_days]
    if len(parts) >= 3:
        last_tok = parts[-1]
        second_last_tok = parts[-2]
        if last_tok.isdigit():
            days = int(last_tok, 10)
            if days < 1:
                raise ValueError(f"staleness days must be >= 1, got {days}")
            if second_last_tok.isdigit():
                m_val = int(second_last_tok, 10)
                m = m_val if m_val >= 1 else None
                url = " ".join(parts[:-2]).strip()
                if not url:
                    raise ValueError("missing URL")
                return FeedSpec(url=url, depth_m=m, max_age_days=days)
            elif second_last_tok in ("-", "0", "none", "None", "all"):
                url = " ".join(parts[:-2]).strip()
                if not url:
                    raise ValueError("missing URL")
                return FeedSpec(url=url, depth_m=None, max_age_days=days)

    # Check for 2+ parts: URL [M]
    if len(parts) >= 2 and parts[-1].isdigit():
        depth = int(parts[-1], 10)
        if depth < 1:
            raise ValueError(f"feed depth M must be >= 1, got {depth}")
        url = " ".join(parts[:-1]).strip()
        if not url:
            raise ValueError("missing URL before feed depth M")
        return FeedSpec(url=url, depth_m=depth, max_age_days=None)

    return FeedSpec(url=" ".join(parts).strip(), depth_m=None, max_age_days=None)


def load_feed_specs_from_file(path: Path) -> list[FeedSpec]:
    """One feed spec per line; # starts a comment to end of line; blank lines skipped."""
    text = path.read_text(encoding="utf-8")
    specs: list[FeedSpec] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            spec = parse_feed_spec_line(line)
        except ValueError as e:
            raise ValueError(f"{path}:{lineno}: {e}") from e
        if spec.url:
            specs.append(spec)
    return specs


def resolve_feed_specs(
    feeds_file: Path | None,
    extra_feeds: list[str] | None,
) -> tuple[list[FeedSpec], str]:
    """Returns (feed specs, description of source for messages)."""
    script_feeds_txt = script_dir() / "feeds.txt"
    extras_specs = [
        FeedSpec(url=u.strip(), depth_m=None, max_age_days=None)
        for u in (extra_feeds or [])
        if u.strip()
    ]

    if feeds_file is not None:
        path = feeds_file.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        from_file = load_feed_specs_from_file(path)
        if not from_file:
            raise ValueError(f"No feed URLs in file: {path}")
        merged = _dedupe_specs_preserve_order(from_file + extras_specs)
        return merged, str(path)

    if script_feeds_txt.is_file():
        from_file = load_feed_specs_from_file(script_feeds_txt)
        if not from_file:
            raise ValueError(f"No feed URLs in file: {script_feeds_txt}")
        merged = _dedupe_specs_preserve_order(from_file + extras_specs)
        return merged, str(script_feeds_txt)

    if extras_specs:
        return _dedupe_specs_preserve_order(extras_specs), "(--feed only)"

    return list(DEFAULT_FEED_SPECS), "built-in default"


def _dedupe_specs_preserve_order(specs: list[FeedSpec]) -> list[FeedSpec]:
    seen_u: set[str] = set()
    out: list[FeedSpec] = []
    for spec in specs:
        u = spec.url.strip()
        if not u or u in seen_u:
            continue
        seen_u.add(u)
        out.append(spec)
    return out


def collect_new_items(
    feed_specs: list[FeedSpec],
    already_seen: set[str],
) -> list[tuple[str, str, str | None]]:
    """Scan all feeds; return (fingerprint, article_url, title) not yet seen (deduped within this run).

    Optional M per feed: only the first M entries from the parsed feed document are considered
    (typically the M newest posts). Global per-run cap N is ``--limit`` / ``--max-add`` in main().
    """
    pending_fp: set[str] = set()
    to_send: list[tuple[str, str, str | None]] = []

    for spec in feed_specs:
        feed_url = spec.url
        entry_depth_m = spec.depth_m
        parsed = feedparser.parse(feed_url)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            print(
                f"Feed parse warning for {feed_url!r}: "
                f"{getattr(parsed, 'bozo_exception', 'unknown')}",
                file=sys.stderr,
            )

        entries = list(parsed.entries or [])
        if entry_depth_m is not None:
            entries = entries[:entry_depth_m]
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
        help="Feeds file: one URL per line, optional trailing depth M and staleness days: "
        "\"URL\", \"URL M\", or \"URL M days\". "
        "M = only consider the first M entries from that feed's XML (usually the M newest). "
        "days = prune unread Instapaper bookmarks from that source older than days. "
        "# comments allowed. "
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
        "--skip-prune",
        action="store_true",
        help="Skip staleness pruning even if staleness days is specified in feeds.txt",
    )
    p.add_argument(
        "--max-add",
        "--limit",
        type=int,
        default=0,
        dest="max_add",
        metavar="N",
        help="Per-run cap N: add at most N new articles this run in total across all feeds (0 = no cap; same as --limit). "
        "Applied after each feed's optional depth M window.",
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

    user = os.environ.get("INSTAPAPER_USERNAME", "").strip() or os.environ.get("username", "").strip()
    password = os.environ.get("INSTAPAPER_PASSWORD", "") or os.environ.get("password", "")
    consumer_key = (
        os.environ.get("CONSUMER_KEY", "").strip()
        or os.environ.get("INSTAPAPER_CONSUMER_KEY", "").strip()
    )
    consumer_secret = (
        os.environ.get("CONSUMER_SECRET", "").strip()
        or os.environ.get("INSTAPAPER_CONSUMER_SECRET", "").strip()
    )

    if not args.dry_run and not user:
        print("INSTAPAPER_USERNAME is required (or use --dry-run).", file=sys.stderr)
        return 2

    try:
        feed_specs, source_label = resolve_feed_specs(args.feeds_file, args.extra_feeds)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"Feeds ({len(feed_specs)}): from {source_label}")

    pruned = 0
    if not args.skip_prune:
        pruned = prune_stale_bookmarks(
            feed_specs=feed_specs,
            username=user,
            password=password,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            dry_run=args.dry_run,
            sleep_seconds=args.sleep_seconds,
        )

    to_send = collect_new_items(feed_specs, seen)

    if args.max_add > 0:
        to_send = to_send[: args.max_add]

    if not to_send:
        print(f"No new items to add. Pruned {pruned} item(s).")
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
        print(f"--dry-run: {len(to_send)} new item(s) would be sent, {pruned} item(s) would be pruned.")
    else:
        print(f"Done. Added {added} item(s), pruned {pruned} item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
