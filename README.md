# instadd

Poll RSS/Atom feeds and add new article URLs to [Instapaper](https://www.instapaper.com/) using Instapaper’s Simple API (`/api/add`). Items are deduplicated by a stable fingerprint (feed `guid`/`id` when present, otherwise the article link) stored in a local JSON state file so reruns and cron do not resend the same story.

Optionally prunes stale unread bookmarks from Instapaper using Instapaper's Full API (OAuth 1.0a / xAuth) based on per-feed age limits configured in `feeds.txt`.

## Setup

```bash
cd /path/to/instadd
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env: INSTAPAPER_USERNAME and INSTAPAPER_PASSWORD (empty if your account has no password)
# For staleness pruning, also set CONSUMER_KEY and CONSUMER_SECRET
chmod 600 .env
chmod +x run-cron.sh
```

## Run

```bash
.venv/bin/python rss_to_instapaper.py --dry-run --limit 5   # no Instapaper calls; cap output
.venv/bin/python rss_to_instapaper.py --limit 15           # real adds (needs .env or exported vars)
```

Or:

```bash
./run-cron.sh --limit 15    # loads .env from this directory (or INSTADD_ENV_FILE)
```

Flags that matter:


| Flag                        | Purpose                                                                               |
| --------------------------- | ------------------------------------------------------------------------------------- |
| `--limit N` / `--max-add N` | **Per-run cap N:** at most **N** Instapaper adds **this run** in total (0 = no cap)  |
| `--feeds-file PATH`         | Use that file instead of the default `feeds.txt` next to the script                   |
| `--feed URL`                | Extra feed URL (repeatable); merged after the feeds file                              |
| `--state-file PATH`         | Store fingerprints here instead of the default under `$HOME`                          |
| `--dry-run`                 | Parse feeds and simulate pruning only; no Instapaper mutations, no state updates      |
| `--skip-prune`              | Skip staleness pruning step even if staleness days is set in `feeds.txt`              |


## Feeds & Staleness Pruning

Default behavior:

1. If `feeds.txt` exists next to `rss_to_instapaper.py`, read it (see format below).
2. Else use a small built-in default list.

`**feeds.txt` format:** `URL [M] [staleness_days]`

- `https://example.com/feed/` — consider every entry the feed returns (no depth cap, no pruning).
- `https://example.com/feed/ 25` — optional **M = 25:** only look at the **first 25 entries** in that feed’s parsed document. In typical RSS/Atom ordering that is the **25 newest** items in the XML. Anything older in the file is ignored (including on later runs) unless it moves back into that top **M** window when the feed updates.
- `https://example.com/feed/ 25 14` — depth **M = 25** and **staleness = 14 days:** prune unread bookmarks in Instapaper from this feed that are older than 14 days.
- `https://example.com/feed/ - 14` — no depth cap (`-` or `0`), but prune items older than 14 days.

`#` starts a comment (whole-line or after whitespace); blank lines are ignored.

Order is preserved. Duplicate URLs: first line wins (including its **M** and staleness settings).

`**--feed URL`** extras have **no** depth **M** or staleness pruning. They are merged after the file; duplicate URLs are dropped.

**Pruning Requirements:** Staleness pruning uses Instapaper's Full API and requires `CONSUMER_KEY` and `CONSUMER_SECRET` in `.env`. If these are omitted, pruning is safely skipped with a notice.

## State (what was already sent)

Unless you pass `--state-file`, fingerprints are stored in:

- `$XDG_STATE_HOME/rss-to-instapaper/seen.json` if `XDG_STATE_HOME` is set, else
- `~/.local/state/rss-to-instapaper/seen.json`

That path is tied to the **Unix user** running the script (e.g. your cron user). To keep state inside this repo, use `--state-file /path/to/instadd/seen.json` in cron or in `run-cron.sh`.

## Cron

`/etc/crontab` (system table) example; note the **username** column before the command:

```cron
20 19 * * * <user> /home/<user>/Projects/instadd/run-cron.sh --limit 15 >> /home/<user>/Projects/instadd/cron.log 2>&1
```

User crontab (`crontab -e`) has **no** username field.

## Logs

**This project:** stdout/stderr from the wrapper go wherever you redirect them (above: `cron.log` in the repo). Inspect with `tail -f cron.log`.

**Cron daemon (did the job start, which user, MTA warnings):** on systemd hosts:

```bash
sudo journalctl -t CRON --since "10 min ago"
```

On many Debian/Ubuntu systems you can also:

```bash
sudo grep CRON /var/log/syslog | tail
```

## Requirements

- Python 3 with `feedparser`, `requests`, and `requests-oauthlib` (see `requirements.txt`).
- Network access to the feed URLs and `https://www.instapaper.com/`.

