# instadd

Poll RSS/Atom feeds and add new article URLs to [Instapaper](https://www.instapaper.com/) using Instapaper’s Simple API (`/api/add`). Items are deduplicated by a stable fingerprint (feed `guid`/`id` when present, otherwise the article link) stored in a local JSON state file so reruns and cron do not resend the same story.

## Setup

```bash
cd /path/to/instadd
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env: INSTAPAPER_USERNAME and INSTAPAPER_PASSWORD (empty if your account has no password)
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


| Flag                        | Purpose                                                                      |
| --------------------------- | ---------------------------------------------------------------------------- |
| `--limit N` / `--max-add N` | After per-feed caps: add at most **N** items **total** this run (0 = no cap) |
| `--feeds-file PATH`         | Use that file instead of the default `feeds.txt` next to the script          |
| `--feed URL`                | Extra feed URL (repeatable); merged after the feeds file                     |
| `--state-file PATH`         | Store fingerprints here instead of the default under `$HOME`                 |
| `--dry-run`                 | Parse feeds only; no Instapaper, no state updates                            |


## Feeds

Default behavior:

1. If `feeds.txt` exists next to `rss_to_instapaper.py`, read it (see format below).
2. Else use a small built-in default list.

`**feeds.txt` format:** one feed per line.

- `https://example.com/feed/` — no per-feed cap for that source.
- `https://example.com/feed/ 5` — queue at most **5 new** items from that feed **per run** (fingerprints already in `seen.json` do not count toward the 5). Omit the number for no cap on that feed.

`#` starts a comment (whole-line or after whitespace); blank lines are ignored.

Order is preserved. Duplicate URLs: first line wins (including its cap).

`**--feed URL`** extras have **no** per-feed cap (same as an uncapped line). They are merged after the file; duplicate URLs are dropped.

**Limits stack:** each feed’s optional cap runs first; then `**--limit` / `--max-add`** trims the **combined** queue for that run. Example: two feeds capped at 5 each could produce up to 10 candidates; `--limit 7` would send at most seven overall.

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

- Python 3 with `feedparser` and `requests` (see `requirements.txt`).
- Network access to the feed URLs and `https://www.instapaper.com/api/add`.

