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


| Flag                        | Purpose                                                             |
| --------------------------- | ------------------------------------------------------------------- |
| `--limit N` / `--max-add N` | Add at most N new articles this run (0 = no cap)                    |
| `--feeds-file PATH`         | Use that file instead of the default `feeds.txt` next to the script |
| `--feed URL`                | Extra feed URL (repeatable); merged after the feeds file            |
| `--state-file PATH`         | Store fingerprints here instead of the default under `$HOME`        |
| `--dry-run`                 | Parse feeds only; no Instapaper, no state updates                   |


## Feeds

Default behavior:

1. If `feeds.txt` exists next to `rss_to_instapaper.py`, read it (one URL per line; `#` starts a comment; blank lines ignored).
2. Else use a small built-in default list.

To add a feed: append its RSS or Atom URL as a new line in `feeds.txt`, or pass `--feed https://...` once or multiple times, or point `--feeds-file` at another list.

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

Look for a line like `(kirk) CMD (...run-cron.sh...)`. Messages such as “No MTA installed, discarding output” refer to jobs whose output was not redirected to a file and could not be mailed; they are not about `cron.log` itself.

## Requirements

- Python 3 with `feedparser` and `requests` (see `requirements.txt`).
- Network access to the feed URLs and `https://www.instapaper.com/api/add`.

