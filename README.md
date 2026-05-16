# Leopold EDGAR Watch

Event-driven monitor for SEC filings from Situational Awareness LP (CIK 0002045724, Leopold Aschenbrenner's fund). When a new 13F / 13D / 13G / NT-13F lands, an EC2 poller fires an Anthropic Cloud Routine that parses the filing, diffs vs prior state, and returns a structured alert payload + narrative.

This repo is the agent's memory. The agent reads it on every fire and writes back updates.

## Stack

| Layer | Choice |
|---|---|
| **Trigger** | Anthropic Cloud Routine, API trigger type |
| **Poller** | Python script on rubicon-bot EC2 (13.236.39.21), cron every 5 min |
| **Memory** | This repo (committed back on every fire) |
| **Source** | SEC EDGAR Atom RSS + submissions JSON |
| **Output** | JSON payload + narrative, returned to the poller for downstream alerting |

## Structure

```
leopold-edgar-watch/
├── README.md
├── .gitignore
├── memory/
│   ├── claude.md              identity, rules, schemas, workflow
│   ├── thesis.md              per-ticker thesis bucket for new-position enrichment
│   ├── cusip_lookup.json      CUSIP to ticker mapping (append-only)
│   ├── holdings_history.json  per-quarter snapshot of every position ever held
│   └── alerts/                emitted alerts, newest filename last (YYYY-MM-DD-HHMM-<accession>.md)
├── routines/
│   └── process-filing.md      the routine prompt
└── .github/
    └── workflows/
        └── auto-merge-claude-prs.yml   auto-merges any PR from claude/* branches
```

## SEC endpoints used by the poller

```
# Atom RSS feed for ALL filings from this CIK:
https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0002045724&type=&dateb=&owner=include&count=40&output=atom

# JSON submissions endpoint:
https://data.sec.gov/submissions/CIK0002045724.json
```

Both require a `User-Agent` header. Use a real email per SEC fair-access policy.

## What alerts look like

Every fire produces a Markdown file in `memory/alerts/` with the JSON payload in a code block followed by a 100 to 200 word narrative. The poller also receives both as the routine's return value, ready to template into Telegram, the Hub backend, or email.

## What this agent does NOT do

- Place trades.
- Deliver alerts. The caller handles Telegram / Hub / email.
- Touch unrelated repos or filings outside this CIK.

## Source spec

`/Users/twilly/Obsidian/shared folder with team/01-Rubicon Videos/Video-Plans/2026-05-16-leopold-aschenbrenner/tracking-spec.md`
