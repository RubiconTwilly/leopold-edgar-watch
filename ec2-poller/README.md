# Leopold EDGAR Watch — EC2 poller

Lives at `/home/ubuntu/leopold-watcher/` on rubicon-bot EC2 (13.236.39.21).

## What it does (every 5 min via cron)

1. Polls SEC EDGAR Atom feed for CIK 0002045724.
2. For any new accession_no, POSTs to the Anthropic Cloud Routine.
3. Pulls the watch repo from GitHub.
4. For any new `memory/alerts/<basename>.txt` file in the repo, sends the text to the Rubicon Inner Circle Announcements topic via Telegram (parse_mode Markdown so the title bolds).
5. Publishes the matching `.html` to `/var/www/html/leopold/alerts/` and edits the Telegram message to append a "Full view" link.

## Files

- `poll.py` — main script
- `.env` — secrets (NOT in repo)
- `state.json` — tracks `seen_accessions`, `dispatched_alerts`, `message_ids`
- `poll.log` — append-only log
- `repo/` — local clone of `RubiconTwilly/leopold-edgar-watch` (auto-pulled each run)

## Required env vars (`.env`)

```
LEOPOLD_ROUTINE_URL=https://api.anthropic.com/v1/claude_code/routines/<id>/fire
LEOPOLD_ROUTINE_TOKEN=sk-ant-oat01-...
BOT_TOKEN=<rubicon-bot Telegram BOT_TOKEN; reused from /home/ubuntu/rubiconbot/.env>
```

## Crontab entry

```
*/5 * * * * /usr/bin/python3 /home/ubuntu/leopold-watcher/poll.py
```

## Manual test fire (skip EDGAR, fire routine directly with a known filing)

```
python3 -c "
import json, urllib.request
env = {l.split('=',1)[0]: l.split('=',1)[1].strip() for l in open('/home/ubuntu/leopold-watcher/.env') if '=' in l}
payload = {
  'accession_no': '0002045724-26-000002',
  'filing_type': '13F-HR',
  'filed_date': '2026-02-11',
  'filing_url': 'https://www.sec.gov/Archives/edgar/data/2045724/000204572426000002/0002045724-26-000002-index.htm',
  'period_of_report': '2025-12-31',
}
req = urllib.request.Request(env['LEOPOLD_ROUTINE_URL'], data=json.dumps(payload).encode(), method='POST',
  headers={'Authorization': 'Bearer ' + env['LEOPOLD_ROUTINE_TOKEN'], 'Content-Type': 'application/json'})
print(urllib.request.urlopen(req, timeout=30).read().decode())
"
```
