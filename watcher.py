"""Fixr Ping Messager - watch web pages and push to your phone on change.

Run once (a single check):
    python watcher.py

Run continuously, checking every config.interval_seconds:
    python watcher.py --loop

Fixr organiser pages are fetched with a fast, lightweight HTTP request (the
event data is server-rendered into the page's __NEXT_DATA__ JSON, so no browser
is needed). Other pages fall back to a real headless browser (Playwright).

State (last-seen content) is stored in ./state/ so changes are detected across
runs.
"""
import argparse
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import threading
import time
from datetime import datetime

import requests

from notify import send_push

ROOT = pathlib.Path(__file__).parent
STATE_DIR = ROOT / "state"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Reuse one connection (keep-alive): faster and gentler on the server.
SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)

HEARTBEAT_SECONDS = 300  # in --loop mode, log "still alive" at most this often
_NEXT_DATA_RE = re.compile(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def fixr_event_label(ev):
    """Best-effort human label for one Fixr event object."""
    if not isinstance(ev, dict):
        return str(ev)
    name = ev.get("name") or ev.get("title") or "Event"
    date = (
        ev.get("openTime") or ev.get("open_time")
        or ev.get("startTime") or ev.get("start_time")
        or ev.get("date") or ""
    )
    return f"{name} @ {date}".strip(" @")


def fixr_signature(data):
    """Build (signature, items) from a Fixr organiser 'data' object.

    signature changes whenever the event payload changes; items are
    human-readable labels for the notification.
    """
    events = data.get("data", [])
    count = data.get("count", len(events))
    sig_obj = {
        "count": count,
        "events": sorted(
            json.dumps(e, sort_keys=True, ensure_ascii=False) for e in events),
    }
    signature = json.dumps(sig_obj, ensure_ascii=False)
    items = sorted(fixr_event_label(e) for e in events) if events else []
    return signature, items


def _fetch_next_data(url):
    """GET a Fixr page and return its parsed __NEXT_DATA__ JSON blob."""
    r = SESSION.get(url, timeout=15)
    if r.status_code == 429:
        raise RuntimeError("rate limited (HTTP 429) - increase interval_seconds")
    r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise RuntimeError("could not find __NEXT_DATA__ in page HTML")
    return json.loads(m.group(1))


def fetch_fixr_organiser(url):
    """Fast path for a Fixr organiser page: events live at pageProps.data."""
    data = _fetch_next_data(url)["props"]["pageProps"]["data"]
    return fixr_signature(data)


def fetch_fixr_venue(url):
    """Fast path for a Fixr venue page: events live at pageProps.venue.events."""
    venue = _fetch_next_data(url)["props"]["pageProps"]["venue"]
    events = venue.get("events") or []
    count = venue.get("futureEvents", len(events))
    return fixr_signature({"data": events, "count": count})


_HN_TITLE_RE = re.compile(r'<span class="titleline">\s*<a\b[^>]*>(.*?)</a>', re.S)


def fetch_hn_newest(url):
    """Fast path for Hacker News: extract the list of newest story titles.

    Used as a frequently-updating test target.
    """
    r = SESSION.get(url, timeout=15)
    if r.status_code == 429:
        raise RuntimeError("rate limited (HTTP 429) - increase interval_seconds")
    r.raise_for_status()
    titles = [html.unescape(t).strip() for t in _HN_TITLE_RE.findall(r.text)]
    titles = sorted(t for t in titles if t)
    signature = json.dumps(titles, ensure_ascii=False)
    return signature, titles


def fetch_with_browser(page_cfg, browser):
    """Slower path for non-Fixr pages: render with Playwright."""
    selector = page_cfg.get("selector")
    context = browser.new_context(user_agent=USER_AGENT)
    page = context.new_page()
    page.goto(page_cfg["url"], wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1500)
    signature = page.inner_text(selector) if selector else page.inner_text("body")
    context.close()
    return signature.strip(), None


# Modes that use a fast HTTP fetch instead of a headless browser.
LIGHTWEIGHT_FETCHERS = {
    "fixr_organiser": fetch_fixr_organiser,
    "fixr_venue": fetch_fixr_venue,
    "hn_newest": fetch_hn_newest,
}


def fetch_page_content(page_cfg, browser):
    fetcher = LIGHTWEIGHT_FETCHERS.get(page_cfg.get("mode"))
    if fetcher:
        return fetcher(page_cfg["url"])
    return fetch_with_browser(page_cfg, browser)


def page_needs_browser(page_cfg):
    return page_cfg.get("mode") not in LIGHTWEIGHT_FETCHERS


def state_file(page_cfg):
    return STATE_DIR / f"{slug(page_cfg['name'])}.json"


def save_state(sf, digest, items, now):
    sf.write_text(
        json.dumps({"hash": digest, "items": items, "seen": now}, indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )


def check_page(page_cfg, browser, ntfy, log_nochange=True):
    """Check one page. Returns one of: 'baseline', 'nochange', 'changed'."""
    name = page_cfg["name"]
    signature, items = fetch_page_content(page_cfg, browser)
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    sf = state_file(page_cfg)
    prev = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else None
    now = datetime.now().isoformat(timespec="seconds")

    if prev is None:
        n_items = len(items) if items is not None else "n/a"
        save_state(sf, digest, items, now)
        print(f"[{now}] {name}: baseline saved ({n_items} items).")
        return "baseline"

    if prev["hash"] == digest:
        if log_nochange:
            print(f"[{now}] {name}: no change.")
        return "nochange"

    added = []
    if items is not None and prev.get("items") is not None:
        added = [x for x in items if x not in set(prev["items"])]
    if added:
        message = "New: " + " | ".join(added[:5])
        if len(added) > 5:
            message += f" (+{len(added) - 5} more)"
    else:
        message = "The page content changed."

    print(f"[{now}] {name}: CHANGED -> {message}")
    send_push(ntfy["server"], ntfy["topic"], title=f"Fixr Ping: {name}",
              message=message, url=page_cfg["url"], priority="high")
    save_state(sf, digest, items, now)
    return "changed"


def run_once(cfg, log_nochange=True):
    """Check every enabled page once. Returns the number of errors."""
    STATE_DIR.mkdir(exist_ok=True)
    ntfy = cfg["ntfy"]
    pages = [p for p in cfg["pages"] if p.get("enabled", True)]
    errors = 0

    # Only spin up a browser if some page actually needs one.
    browser = pw = None
    if any(page_needs_browser(p) for p in pages):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)

    try:
        for page_cfg in pages:
            try:
                check_page(page_cfg, browser, ntfy, log_nochange=log_nochange)
            except Exception as e:
                errors += 1
                print(f"  ! Error checking '{page_cfg['name']}': {e}",
                      file=sys.stderr)
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
    return errors


class _Tee:
    """Write to several streams at once (e.g. console + log file)."""
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def run_loop(cfg, stop_event=None, write_pid=True):
    """Check all pages repeatedly until stopped.

    stop_event: a threading.Event; when set, the loop exits promptly (the GUI
                uses this). If None, runs until KeyboardInterrupt.
    write_pid:  record this process's PID in state/watcher.pid (single-instance
                guard).
    """
    if stop_event is None:
        stop_event = threading.Event()  # never set -> runs until Ctrl+C
    interval = cfg.get("interval_seconds", 60)
    enabled = sum(1 for p in cfg["pages"] if p.get("enabled", True))
    STATE_DIR.mkdir(exist_ok=True)
    pid_file = STATE_DIR / "watcher.pid"
    if write_pid:
        pid_file.write_text(str(os.getpid()))
    print(f"Watching {enabled} page(s), every {interval}s.")
    backoff = interval
    checks = 0
    last_heartbeat = time.monotonic()
    try:
        while not stop_event.is_set():
            # Quiet on 'no change' to keep the log small at high frequency.
            errors = run_once(cfg, log_nochange=False)
            checks += 1

            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                ts = datetime.now().isoformat(timespec="seconds")
                print(f"[{ts}] heartbeat: {checks} checks done, still watching.")
                last_heartbeat = now

            if errors:
                backoff = min(max(backoff * 2, 5), 300)
                print(f"  (errors; backing off to {backoff}s)")
                stop_event.wait(backoff)  # interruptible sleep
            else:
                backoff = interval
                stop_event.wait(interval)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if write_pid:
            try:
                pid_file.unlink()
            except OSError:
                pass


def main():
    # Always keep a log trail (Task Scheduler runs have no visible console),
    # while still printing to the terminal for manual runs.
    logf = open(ROOT / "watcher.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, logf)
    sys.stderr = _Tee(sys.__stderr__, logf)

    parser = argparse.ArgumentParser(description="Fixr Ping Messager")
    parser.add_argument("--loop", action="store_true",
                        help="keep running, checking every interval_seconds")
    args = parser.parse_args()

    cfg = load_config()
    if args.loop:
        run_loop(cfg)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
