#!/usr/bin/env python3
"""
Leopold EDGAR Watch — poller.

Lives on rubicon-bot EC2 at /home/ubuntu/leopold-watcher/, managed by PM2.

Architecture:
  Tight loop (every 15s). Each tick:
    1) Poll SEC EDGAR Atom feed for CIK 0002045724.
    2) For any unseen accession_no:
        a) Quick-parse the filing locally (infoTable XML, just enough to list tickers).
        b) Send ALERT 1 to Telegram Announcements: "It is live. New / Old / Quick diff. Researching..."
        c) Fire the Cloud Routine for the full research.
        d) Save state.
    3) Pull the watch repo. For any new alert .txt file landed by the routine:
        a) Send ALERT 2 as an EDIT to the ALERT 1 message in Telegram.
        b) Append a Full View link (GitHub Pages URL of the .html sibling).
        c) Mark dispatched.

Run modes:
  python3 poll.py            -> one tick, exit (for ad-hoc testing)
  python3 poll.py --daemon   -> loop forever with 15s sleep (PM2 starts this)
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Belt-and-braces: urllib's per-request timeout sometimes leaks on partial reads.
# A global default ensures the socket itself cannot hang forever.
socket.setdefaulttimeout(25)

# -------------------- config --------------------

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
STATE_PATH = ROOT / "state.json"
REPO_PATH = ROOT / "repo"
ALERTS_DIR = REPO_PATH / "memory" / "alerts"
LOG_PATH = ROOT / "poll.log"

POLL_INTERVAL_SECONDS = 15

CIK = "0002045724"
ATOM_URL = (
    f"https://www.sec.gov/cgi-bin/browse-edgar?"
    f"action=getcompany&CIK={CIK}&type=&dateb=&owner=include&count=40&output=atom"
)
USER_AGENT = "Tristan Wilson wilsontristan5@gmail.com"

TELEGRAM_CHAT_ID = "-1001806961307"
TELEGRAM_THREAD_ID = "15954"
TELEGRAM_API = "https://api.telegram.org"

REPO_URL = "https://github.com/RubiconTwilly/leopold-edgar-watch.git"
GH_PAGES_BASE = "https://rubicontwilly.github.io/leopold-edgar-watch/memory/alerts"


# -------------------- helpers --------------------

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {
        "seen_accessions": [],
        "dispatched_alerts": [],
        "message_ids": {},        # accession_no -> telegram message_id
        "prior_ticker_snapshot": [],  # used to compute quick diff (set after each new 13F)
    }


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=2))


def http_get(url, ua=USER_AGENT, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout).read()


def http_post(url, body_bytes, headers, timeout=30):
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout).read()


# -------------------- EDGAR Atom + filing parse --------------------

def parse_atom_entries(xml_bytes):
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_bytes)
    out = []
    for entry in root.findall("a:entry", ns):
        content = entry.find("a:content", ns)
        if content is None:
            continue
        acc = content.findtext("accession-number")
        ftype = content.findtext("filing-type")
        fdate = content.findtext("filing-date")
        href = content.findtext("filing-href")
        if acc and ftype:
            out.append({
                "accession_no": acc.strip(),
                "filing_type": ftype.strip(),
                "filed_date": (fdate or "").strip(),
                "filing_url": (href or "").strip(),
                "period_of_report": None,
            })
    return out


def find_infotable_url(index_url):
    """Given a filing index URL, find the infoTable XML.
    Funds use varied naming (informationtable.xml, SALP_13FQ425.xml, etc.).
    Strategy: grab all .xml links NOT under xslForm... viewer paths and NOT primary_doc.xml.
    The remaining xml is the infoTable."""
    try:
        html = http_get(index_url).decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"  ERROR fetching index: {e}")
        return None
    candidates = []
    for href in re.findall(r'href="([^"]+\.xml)"', html, re.I):
        if "xslForm" in href:
            continue
        if href.lower().endswith("primary_doc.xml"):
            continue
        if not href.startswith("/"):
            continue
        candidates.append("https://www.sec.gov" + href)
    if not candidates:
        return None
    return candidates[0]


def quick_parse_13f(filing_url, cusip_lookup):
    """Return list of {ticker, cusip, value, shares, put_call} for the new filing.
    Used to build ALERT 1 instantly. Returns [] if anything fails."""
    info_url = find_infotable_url(filing_url)
    if not info_url:
        return []
    try:
        raw = http_get(info_url).decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"  ERROR fetching infotable: {e}")
        return []
    # Strip namespaces
    raw = re.sub(r'\sxmlns(?::\w+)?="[^"]*"', "", raw)
    raw = re.sub(r'\sxsi:schemaLocation="[^"]*"', "", raw)
    raw = re.sub(r"<(/?)(?:ns\d+|n\d+|xsi|xs):", r"<\1", raw)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        log(f"  ERROR parsing infotable XML: {e}")
        return []
    out = []
    for ent in root.findall(".//infoTable"):
        cusip = (ent.findtext("cusip") or "").strip()
        name = (ent.findtext("nameOfIssuer") or "").strip()
        value = int(ent.findtext("value") or 0)
        shares_text = ent.findtext(".//sshPrnamt") or "0"
        shares = int(shares_text)
        pc = (ent.findtext("putCall") or "").strip()
        ticker = cusip_lookup.get(cusip, f"UNK_{cusip[:6]}")
        out.append({
            "ticker": ticker,
            "cusip": cusip,
            "name": name,
            "value": value,
            "shares": shares,
            "put_call": pc,
        })
    return out


def load_cusip_lookup():
    p = REPO_PATH / "memory" / "cusip_lookup.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


# -------------------- Telegram --------------------

def telegram_send(env, text):
    token = env["BOT_TOKEN"]
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "message_thread_id": TELEGRAM_THREAD_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        raw = http_post(url, body, {"Content-Type": "application/x-www-form-urlencoded"})
        resp = json.loads(raw)
        if resp.get("ok"):
            return resp["result"]["message_id"]
        log(f"  telegram sendMessage failed: {raw[:300]!r}")
    except Exception as e:
        log(f"  ERROR telegram sendMessage: {e}")
    return None


def telegram_edit(env, message_id, text):
    token = env["BOT_TOKEN"]
    url = f"{TELEGRAM_API}/bot{token}/editMessageText"
    body = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        raw = http_post(url, body, {"Content-Type": "application/x-www-form-urlencoded"})
        resp = json.loads(raw)
        if resp.get("ok"):
            return True
        log(f"  telegram editMessage failed: {raw[:300]!r}")
    except Exception as e:
        log(f"  ERROR telegram editMessage: {e}")
    return False


# -------------------- Cloud Routine fire --------------------

def fire_routine(env, payload):
    url = env["LEOPOLD_ROUTINE_URL"]
    token = env["LEOPOLD_ROUTINE_TOKEN"]
    body = json.dumps({"text": json.dumps(payload)}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    log(f"firing routine for {payload['accession_no']} ({payload['filing_type']})")
    try:
        raw = http_post(url, body, headers, timeout=30)
        try:
            resp = json.loads(raw)
            sid = resp.get("claude_code_session_id")
            log(f"  routine fired, session={sid}")
        except Exception:
            log(f"  routine fired, raw: {raw[:200]!r}")
        return True
    except Exception as e:
        log(f"  ERROR firing routine: {e}")
        return False


# -------------------- Git --------------------

def git_sync():
    if not REPO_PATH.exists():
        log(f"cloning {REPO_URL} -> {REPO_PATH}")
        subprocess.run(["git", "clone", "--quiet", REPO_URL, str(REPO_PATH)], check=True)
    else:
        subprocess.run(
            ["git", "-C", str(REPO_PATH), "fetch", "--quiet", "origin", "main"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(REPO_PATH), "reset", "--quiet", "--hard", "origin/main"],
            check=True,
        )


# -------------------- ALERT 1 (quick) --------------------

def format_alert_1(entry, quick_holdings, prior_tickers):
    ftype = entry["filing_type"]
    period = entry.get("period_of_report") or entry["filed_date"]
    acc = entry["accession_no"]
    url = entry["filing_url"]

    if quick_holdings and ftype.startswith("13F"):
        new_tickers = sorted({h["ticker"] for h in quick_holdings})
        prior_set = set(prior_tickers or [])
        added = sorted(set(new_tickers) - prior_set)
        removed = sorted(prior_set - set(new_tickers))
        unchanged_count = len(set(new_tickers) & prior_set)

        lines = [
            f"*Leopold's Holdings Alert ({ftype})*",
            "",
            f"It's live. Filed {entry['filed_date']} (period {period}).",
            "",
            f"Now holding ({len(new_tickers)}): {', '.join(new_tickers)}",
        ]
        if prior_tickers:
            lines += ["", f"Prior holdings ({len(prior_set)}): {', '.join(sorted(prior_set))}"]
        lines += [""]
        if added:
            lines.append(f"➕ Added: {', '.join(added)}")
        if removed:
            lines.append(f"➖ Exited: {', '.join(removed)}")
        if not added and not removed:
            lines.append(f"No ticker-level changes vs prior filing. {unchanged_count} positions held over.")
        lines += [
            "",
            "Full research processing, will edit this message when ready.",
            "",
            f"[Filing on EDGAR]({url})",
        ]
        return "\n".join(lines)

    # Non-13F (13D, 13G, NT-13F, etc.) — minimal announcement
    return "\n".join([
        f"*Leopold's Holdings Alert ({ftype})*",
        "",
        f"It's live. Filed {entry['filed_date']}.",
        f"Accession: {acc}",
        "",
        "Full research processing, will edit this message when ready.",
        "",
        f"[Filing on EDGAR]({url})",
    ])


# -------------------- tick --------------------

def tick(env, state):
    # --- 1) EDGAR check ---
    try:
        xml = http_get(ATOM_URL)
    except Exception as e:
        log(f"ERROR fetching Atom: {e}")
        return
    entries = parse_atom_entries(xml)
    seen = set(state["seen_accessions"])
    new = [e for e in entries if e["accession_no"] not in seen]

    if new:
        # Make sure repo is current before quick-parse (need cusip_lookup)
        try:
            git_sync()
        except Exception as e:
            log(f"WARN git sync before quick-parse failed: {e}")
        cusip_lookup = load_cusip_lookup()

        for entry in reversed(new):  # oldest first
            log(f"new filing: {entry['accession_no']} {entry['filing_type']}")
            quick_holdings = []
            if entry["filing_type"].startswith("13F"):
                quick_holdings = quick_parse_13f(entry["filing_url"], cusip_lookup)
                log(f"  quick-parsed {len(quick_holdings)} positions")

            msg1 = format_alert_1(entry, quick_holdings, state.get("prior_ticker_snapshot"))
            msg_id = telegram_send(env, msg1)
            if msg_id:
                state["message_ids"][entry["accession_no"]] = msg_id
                log(f"  ALERT 1 sent (msg_id={msg_id})")
            else:
                log(f"  ALERT 1 send failed; will retry next tick")
                continue  # don't mark seen; retry next tick

            fire_routine(env, entry)

            if quick_holdings:
                state["prior_ticker_snapshot"] = sorted({h["ticker"] for h in quick_holdings})
            state["seen_accessions"].append(entry["accession_no"])
            save_state(state)

    # --- 2) Alert dispatch (ALERT 2 = edit) ---
    try:
        git_sync()
    except Exception as e:
        log(f"ERROR git sync: {e}")
        return
    if not ALERTS_DIR.exists():
        return
    txt_files = sorted([p for p in ALERTS_DIR.glob("*.txt")])
    dispatched = set(state["dispatched_alerts"])
    for txt in txt_files:
        basename = txt.stem
        if basename in dispatched:
            continue
        # Extract accession from filename (YYYY-MM-DD-HHMM-<accession>)
        m = re.match(r"\d{4}-\d{2}-\d{2}-\d{4}-(.+)$", basename)
        if not m:
            log(f"  cannot parse accession from {basename}, skipping")
            state["dispatched_alerts"].append(basename)
            save_state(state)
            continue
        accession = m.group(1)
        msg_id = state["message_ids"].get(accession)
        if not msg_id:
            log(f"  no message_id for {accession}; sending fresh instead of edit")
            full_text = txt.read_text()
            html_url = f"{GH_PAGES_BASE}/{basename}.html"
            edited = full_text + f"\n\n[Full view]({html_url})"
            new_id = telegram_send(env, edited)
            if new_id:
                state["dispatched_alerts"].append(basename)
                save_state(state)
            continue

        full_text = txt.read_text()
        html_url = f"{GH_PAGES_BASE}/{basename}.html"
        edited = full_text + f"\n\n[Full view]({html_url})"
        if telegram_edit(env, msg_id, edited):
            log(f"  ALERT 2 edit pushed for {accession}")
            state["dispatched_alerts"].append(basename)
            save_state(state)


# -------------------- entrypoint --------------------

def main():
    env = load_env()
    missing = [k for k in ("LEOPOLD_ROUTINE_URL", "LEOPOLD_ROUTINE_TOKEN", "BOT_TOKEN") if k not in env]
    if missing:
        log(f"FATAL: missing env vars: {missing}")
        sys.exit(1)

    daemon = "--daemon" in sys.argv
    if not daemon:
        state = load_state()
        tick(env, state)
        return

    log(f"daemon start, loop interval {POLL_INTERVAL_SECONDS}s")
    tick_count = 0
    while True:
        state = load_state()
        try:
            tick(env, state)
        except Exception:
            log("UNCAUGHT in tick:\n" + traceback.format_exc())
        tick_count += 1
        if tick_count % 240 == 0:  # every ~hour at 15s interval
            log(f"heartbeat: {tick_count} ticks completed, seen={len(state['seen_accessions'])}, dispatched={len(state['dispatched_alerts'])}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
