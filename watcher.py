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
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

import requests

import reserve
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


# Browsers to open new events in (config key "browsers"). Each is looked up in
# the usual install locations; any that aren't installed are skipped.
DEFAULT_BROWSERS = ["chrome", "operagx"]
_BROWSER_CANDIDATES = {
    "chrome": {
        "win": [r"Google\Chrome\Application\chrome.exe"],
        "app_path": "chrome.exe",
        "mac": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "path": ["google-chrome", "google-chrome-stable", "chrome", "chromium"],
    },
    "operagx": {
        "win": [r"Programs\Opera GX\opera.exe",
                r"Programs\Opera GX\launcher.exe",
                r"Opera GX\opera.exe",
                r"Opera GX\launcher.exe"],
        "app_path": "opera.exe",
        "mac": ["/Applications/Opera GX.app/Contents/MacOS/Opera"],
        "path": ["opera-gx", "opera"],
    },
    "edge": {
        "win": [r"Microsoft\Edge\Application\msedge.exe"],
        "app_path": "msedge.exe",
        # Built-in Windows link that always opens Edge, used if msedge.exe
        # can't be located.
        "protocol": "microsoft-edge:",
        "mac": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "path": ["msedge", "microsoft-edge", "microsoft-edge-stable"],
    },
}


def _registry_app_path(exe_name):
    """Look up a browser's install path in the Windows 'App Paths' registry."""
    try:
        import winreg
    except ImportError:
        return None
    key_path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\"
                + exe_name)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                path = winreg.QueryValue(key, None).strip('"')
            if os.path.isfile(path):
                return path
        except OSError:
            pass
    return None


def _registry_installed_browser(key):
    """Find a browser in Windows' list of installed browsers
    (the one behind Settings > Default apps), e.g. "Opera GXStable"."""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for base in (r"SOFTWARE\Clients\StartMenuInternet",
                     r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"):
            try:
                root = winreg.OpenKey(hive, base)
            except OSError:
                continue
            with root:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    i += 1
                    if key not in _browser_key(sub):
                        continue
                    try:
                        with winreg.OpenKey(root, sub + r"\shell\open\command") as k:
                            cmd = winreg.QueryValue(k, None)
                    except OSError:
                        continue
                    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
                    path = m and (m.group(1) or m.group(2))
                    if path and os.path.isfile(path):
                        return path
    return None


def _browser_key(name):
    """Normalise a browser name, so "Opera GX" / "opera_gx" -> "operagx"."""
    return re.sub(r"[^a-z]", "", name.lower())


def find_browser(name):
    """Return the executable path for a browser name, or None if not found."""
    cands = _BROWSER_CANDIDATES.get(_browser_key(name))
    if cands is None:
        # Not a known name -- treat it as a path or command on PATH.
        return name if os.path.isfile(name) else shutil.which(name)
    if sys.platform == "win32":
        bases = [os.environ.get(env) for env in
                 ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
                  "LOCALAPPDATA")]
        bases += [r"C:\Program Files", r"C:\Program Files (x86)"]
        for base in bases:
            for rel in cands["win"]:
                if base and os.path.isfile(os.path.join(base, rel)):
                    return os.path.join(base, rel)
        found = (_registry_app_path(cands["app_path"])
                 or _registry_installed_browser(_browser_key(name)))
        if found:
            return found
    elif sys.platform == "darwin":
        for path in cands["mac"]:
            if os.path.isfile(path):
                return path
    for cmd in cands["path"]:
        found = shutil.which(cmd)
        if found:
            return found
    return None


def open_in_browsers(url, browsers, fallback=True):
    """Open url in each configured browser; fall back to the system default
    (unless fallback=False) if none of them could be opened."""
    opened = False
    for name in browsers:
        exe = find_browser(name)
        protocol = _BROWSER_CANDIDATES.get(_browser_key(name), {}).get("protocol")
        try:
            if exe:
                subprocess.Popen([exe, url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                print(f"  -> opened {url} in {name} ({exe})")
            elif protocol and sys.platform == "win32":
                os.startfile(protocol + url)
                print(f"  -> opened {url} in {name} (via {protocol})")
            else:
                print(f"  ! {name} not found, skipping")
                continue
            opened = True
        except Exception as e:
            print(f"  ! could not open {name}: {e}")
    if not opened and fallback:
        webbrowser.open(url, new=2)  # new=2 -> new browser tab
        print(f"  -> opened {url} in your default browser")


TEST_URL = "https://fixr.co"


def test_browsers(cfg):
    """Open a test page in every configured browser, to check they all work."""
    browsers = cfg.get("browsers", DEFAULT_BROWSERS)
    print(f"Testing browsers: {', '.join(browsers)}")
    for name in browsers:
        print(f"  browser '{name}': {find_browser(name) or 'NOT FOUND'}")
    open_in_browsers(TEST_URL, browsers)


def test_reserve(cfg, query):
    """Pretend an event matching `query` was just posted on an auto-reserve
    page: send the phone ping and run auto-reserve on it for real."""
    words = query.lower().split()
    pages = [p for p in cfg["pages"] if p.get("auto_reserve")]
    if not pages:
        print("No pages have \"auto_reserve\": true in config.json.")
        return False
    for page_cfg in pages:
        _, items = fetch_page_content(page_cfg, None)
        items = items or []
        match = [it for it in items
                 if all(w in _item_label(it).lower() for w in words)]
        if not match:
            continue
        it = match[0]
        label, url = _item_label(it), _item_url(it)
        print(f"[test] Pretending '{label}' was just posted on "
              f"{page_cfg['name']}.")
        try:
            send_push(cfg["ntfy"]["server"], cfg["ntfy"]["topic"],
                      title=f"TEST Fixr Ping: {page_cfg['name']}",
                      message=f"New: {label}", url=url, priority="high")
        except Exception as e:
            print(f"  ! could not send push: {e}")
        reserve.start(url, label, cfg, _reserve_notifier(cfg["ntfy"]),
                      _open_one, _reserve_browsers(cfg))
        return True
    print(f"[test] No event matching '{query}'. Events found:")
    for page_cfg in pages:
        _, items = fetch_page_content(page_cfg, None)
        for it in items or []:
            print(f"   - {_item_label(it)}")
    return False


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _format_event_time(ev):
    """Render a Fixr event's start time. openTime is a Unix timestamp."""
    t = (ev.get("openTime") or ev.get("open_time")
         or ev.get("startTime") or ev.get("start_time") or ev.get("date"))
    if isinstance(t, (int, float)):
        try:
            return datetime.fromtimestamp(t).strftime("%a %d %b %H:%M")
        except (OSError, ValueError, OverflowError):
            return str(t)
    return t or ""


def fixr_event_label(ev):
    """Best-effort human label for one Fixr event object."""
    if not isinstance(ev, dict):
        return str(ev)
    name = ev.get("name") or ev.get("title") or "Event"
    return f"{name} @ {_format_event_time(ev)}".strip(" @")


def fixr_event_url(ev):
    """Direct link to a Fixr event's ticket-selection screen.

    Fixr serves ticket selection at <event-url>/tickets (the event page's own
    'TICKETS' button links there), so opening that lands straight on it.
    """
    if not isinstance(ev, dict):
        return None
    if ev.get("shareUrl"):
        base = ev["shareUrl"]
    elif ev.get("routingPart"):
        base = f"https://fixr.co/event/{ev['routingPart']}"
    elif ev.get("id"):
        base = f"https://fixr.co/event/{ev['id']}"
    else:
        return None
    return base.rstrip("/") + "/tickets"


def fixr_signature(data):
    """Build (signature, items) from a Fixr organiser 'data' object.

    signature changes whenever the event payload changes; items are
    {'label', 'url'} dicts — the label for the notification, the url (the
    event's own ticket page) for opening in the browser.
    """
    events = data.get("data", [])
    count = data.get("count", len(events))
    sig_obj = {
        "count": count,
        "events": sorted(
            json.dumps(e, sort_keys=True, ensure_ascii=False) for e in events),
    }
    signature = json.dumps(sig_obj, ensure_ascii=False)
    items = sorted(
        ({"label": fixr_event_label(e), "url": fixr_event_url(e)}
         for e in events),
        key=lambda it: it["label"]) if events else []
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


def _item_label(it):
    """An item is either {'label','url'} (Fixr) or a plain string (other)."""
    return it["label"] if isinstance(it, dict) else str(it)


def _item_url(it):
    return it.get("url") if isinstance(it, dict) else None


def _open_one(url, browser):
    """Open url in exactly this browser (no fallback to another one)."""
    open_in_browsers(url, [browser], fallback=False)


def _reserve_browsers(cfg):
    """Browsers auto-reserve runs in: each needs the extension installed."""
    return cfg.get("browsers", DEFAULT_BROWSERS)


def _reserve_notifier(ntfy):
    def notify(title, message, url):
        try:
            send_push(ntfy["server"], ntfy["topic"], title=title,
                      message=message, url=url, priority="max")
        except Exception as e:
            print(f"  ! could not send push: {e}")
    return notify


def check_page(page_cfg, browser, ntfy, log_nochange=True, open_browser=False,
               browsers=DEFAULT_BROWSERS, cfg=None):
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
        prev_labels = {_item_label(it) for it in prev["items"]}
        added = [it for it in items if _item_label(it) not in prev_labels]

    if added:
        labels = [_item_label(it) for it in added]
        message = "New: " + " | ".join(labels[:5])
        if len(labels) > 5:
            message += f" (+{len(labels) - 5} more)"
    else:
        message = "The page content changed."

    print(f"[{now}] {name}: CHANGED -> {message}")

    # New event pages to open (and where the phone notification should link).
    new_urls = [u for u in (_item_url(it) for it in added) if u]
    if new_urls and len(new_urls) <= 8:
        open_targets = new_urls
    else:
        # No specific links, or too many at once -> just open the listing page.
        open_targets = [page_cfg["url"]]
    click_url = new_urls[0] if new_urls else page_cfg["url"]

    auto_reserve = (cfg is not None and page_cfg.get("auto_reserve")
                    and reserve.settings(cfg)["enabled"])
    if auto_reserve:
        # The reserve window opens each new event itself, so don't also open
        # it in the normal browser.
        for it in added:
            if _item_url(it):
                reserve.start(_item_url(it), _item_label(it), cfg,
                              _reserve_notifier(ntfy), _open_one,
                              _reserve_browsers(cfg))
    elif open_browser:
        for target in open_targets:
            try:
                open_in_browsers(target, browsers)
            except Exception as e:
                print(f"  ! could not open browser: {e}")
    try:
        send_push(ntfy["server"], ntfy["topic"], title=f"Fixr Ping: {name}",
                  message=message, url=click_url, priority="high")
    except Exception as e:
        print(f"  ! could not send push: {e}")
    save_state(sf, digest, items, now)
    return "changed"


def run_once(cfg, log_nochange=True):
    """Check every enabled page once. Returns the number of errors."""
    STATE_DIR.mkdir(exist_ok=True)
    ntfy = cfg["ntfy"]
    open_browser = cfg.get("open_browser_on_change", False)
    browsers = cfg.get("browsers", DEFAULT_BROWSERS)
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
                check_page(page_cfg, browser, ntfy, log_nochange=log_nochange,
                           open_browser=open_browser, browsers=browsers,
                           cfg=cfg)
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
    if cfg.get("open_browser_on_change", False):
        for name in cfg.get("browsers", DEFAULT_BROWSERS):
            exe = find_browser(name)
            print(f"  browser '{name}': {exe or 'NOT FOUND'}")
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
    parser.add_argument("--test-browsers", action="store_true",
                        help="open a test page in every configured browser")
    parser.add_argument("--test-reserve", metavar="EVENT",
                        help="run auto-reserve now on the event whose name "
                             "contains EVENT, e.g. \"Thursday Indie Night\"")
    args = parser.parse_args()

    cfg = load_config()
    if args.test_browsers:
        test_browsers(cfg)
    elif args.test_reserve:
        if test_reserve(cfg, args.test_reserve):
            reserve.RESERVER.jobs.join()
            input("Progress appears above. Press Enter to quit when it's "
                  "done...\n")
    elif args.loop:
        run_loop(cfg)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
