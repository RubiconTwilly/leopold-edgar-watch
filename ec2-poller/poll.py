#!/usr/bin/env python3
"""
Leopold EDGAR Watch — poller.

Runs every 5 min from cron on rubicon-bot EC2.

Two concerns in one script:
  1. check_edgar()  : poll SEC Atom feed for new filings from CIK 0002045724;
                      fire the Cloud Routine for each unseen accession_no.
  2. check_alerts() : look in the watch repo's memory/alerts/ for newly-written
                      alert files (the Routine writes JSON + TXT + HTML there);
                      dispatch the TXT to Telegram via rubicon-bot's BOT_TOKEN,
                      copy the HTML to the public webroot, save message_id.

State persisted to /home/ubuntu/leopold-watcher/state.json.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# -------------------- config --------------------

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
STATE_PATH = ROOT / "state.json"
REPO_PATH = ROOT / "repo"  # local clone of leopold-edgar-watch
ALERTS_DIR = REPO_PATH / "memory" / "alerts"
LOG_PATH = ROOT / "poll.log"
PUBLIC_HTML_URL = "https://rubicontwilly.github.io/leopold-edgar-watch/memory/alerts"  # served by GitHub Pages from the repo itself

CIK = "0002045724"
ATOM_URL = (
    f"https://www.sec.gov/cgi-bin/browse-edgar?"
    f"action=getcompany&CIK={CIK}&type=&dateb=&owner=include&count=40&output=atom"
)
USER_AGENT = "Tristan Wilson wilsontristan5@gmail.com"

TELEGRAM_CHAT_ID = "-1001806961307"      # Rubicon Inner Circle group
TELEGRAM_THREAD_ID = "15954"              # Announcements topic
TELEGRAM_API = "https://api.telegram.org"

REPO_URL = "https://github.com/RubiconTwilly/leopold-edgar-watch.git"

# -------------------- helpers --------------------

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


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
    return {"seen_accessions": [], "dispatched_alerts": [], "message_ids": {}}


def save_state(s):
    STATE_PATH.write_text(json.dumps(s, indent=2))


def http_get(url, ua=USER_AGENT, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=timeout).read()


def http_post(url, body_bytes, headers, timeout=120):
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout).read()


# -------------------- EDGAR side --------------------

def parse_atom_entries(xml_bytes):
    """Return list of {accession, filing_type, filed_date, index_url} from the Atom feed."""
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
                "period_of_report": None,  # routine derives this from the filing
            })
    return out


def fire_routine(env, payload):
    """POST to the Cloud Routine /fire endpoint. The API wraps the inner payload as a JSON
    string inside the 'text' field; anthropic-version header is required."""
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
            log(f"  routine fired, raw response: {raw[:200]!r}")
        return True
    except Exception as e:
        log(f"  ERROR firing routine: {e}")
        return False


def check_edgar(env, state):
    """Poll EDGAR; fire routine for any unseen accession_no."""
    try:
        xml = http_get(ATOM_URL)
    except Exception as e:
        log(f"ERROR fetching Atom feed: {e}")
        return
    entries = parse_atom_entries(xml)
    log(f"edgar: {len(entries)} entries in feed, {len(state['seen_accessions'])} previously seen")
    seen = set(state["seen_accessions"])
    new = [e for e in entries if e["accession_no"] not in seen]
    if not new:
        return
    for e in reversed(new):  # fire oldest-first so memory builds in order
        if fire_routine(env, e):
            state["seen_accessions"].append(e["accession_no"])
            save_state(state)


# -------------------- alert dispatch side --------------------

def git_sync():
    """Clone or pull the watch repo so we can read new alert files."""
    if not REPO_PATH.exists():
        log(f"cloning {REPO_URL} -> {REPO_PATH}")
        subprocess.run(["git", "clone", REPO_URL, str(REPO_PATH)], check=True)
    else:
        subprocess.run(["git", "-C", str(REPO_PATH), "fetch", "origin", "main"], check=True)
        subprocess.run(["git", "-C", str(REPO_PATH), "reset", "--hard", "origin/main"], check=True)


def telegram_send(env, text):
    """Send to Announcements topic. Returns message_id or None on failure."""
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
        raw = http_post(url, body, {"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
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
        raw = http_post(url, body, {"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        resp = json.loads(raw)
        if resp.get("ok"):
            return True
        log(f"  telegram editMessage failed: {raw[:300]!r}")
    except Exception as e:
        log(f"  ERROR telegram editMessage: {e}")
    return False


def publish_html(src_path, basename):
    """HTML is auto-served by GitHub Pages from the repo. Return the public URL."""
    return f"{PUBLIC_HTML_URL}/{basename}.html"


def check_alerts(env, state):
    """Look for newly written alert files in the repo; dispatch each."""
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
        message_text = txt.read_text()
        log(f"dispatching alert {basename} ({len(message_text)} chars)")

        # 1) send the plain message
        msg_id = telegram_send(env, message_text)
        if not msg_id:
            log(f"  send failed, not marking dispatched; will retry next run")
            continue
        state["message_ids"][basename] = msg_id

        # 2) publish HTML if present, then edit message to append link
        html_path = ALERTS_DIR / f"{basename}.html"
        if html_path.exists():
            public_url = publish_html(html_path, basename)
            if public_url:
                edited_text = message_text + f"\n\n[Full view]({public_url})"
                telegram_edit(env, msg_id, edited_text)

        state["dispatched_alerts"].append(basename)
        save_state(state)


# -------------------- entrypoint --------------------

def main():
    env = load_env()
    missing = [k for k in ("LEOPOLD_ROUTINE_URL", "LEOPOLD_ROUTINE_TOKEN", "BOT_TOKEN") if k not in env]
    if missing:
        log(f"FATAL: missing env vars: {missing}")
        sys.exit(1)
    state = load_state()
    check_edgar(env, state)
    check_alerts(env, state)


if __name__ == "__main__":
    main()
