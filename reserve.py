"""Auto-reserve a Fixr ticket when a new event is posted.

Drives a real Chrome window (its own profile in ./fixr_profile, so you log into
Fixr once and it stays logged in). For each new event it opens the ticket page
and, one time slot at a time:

  1. adds ONE ticket for that slot and presses reserve/checkout,
  2. if the reservation goes through -> stops, pings your phone, and leaves the
     window open on the basket so you can pay,
  3. if it fails -> removes the ticket, checks it's gone, then tries the next
     slot.

Slot order: the preferred start times in config (10:00pm, 9:30pm, ... 8:00pm by
default), then any later slots, earliest first. Slots before the last
preferred time are never tried.

NOTE: this was written without being able to see Fixr's ticket page, so the
button/label matching below is a best guess -- the activity feed logs each
step so it can be fixed up against the real site.
"""
import pathlib
import queue
import re
import threading
import time

ROOT = pathlib.Path(__file__).parent
PROFILE_DIR = ROOT / "fixr_profile"
LOGIN_URL = "https://fixr.co/login"

DEFAULTS = {
    "enabled": True,
    # Try these start times first, in this order (24h clock)...
    "preferred_start_times": ["22:00", "21:30", "21:00", "20:30", "20:00"],
    # ...then any slot starting after the first preferred time, earliest first.
    "then_try_later_slots": True,
    # Tickets may not be on sale the moment an event appears: keep reloading
    # the ticket page for up to this long looking for time-slot tickets.
    "wait_for_tickets_minutes": 10,
    # Seconds to wait for a reservation to go through before calling it failed.
    "reserve_timeout_seconds": 15,
    "headless": False,
}

# e.g. "10:00pm - 10:30pm", "10-10:30PM", "22:00 – 22:30", "9.30pm to 10pm"
_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?"
SLOT_RE = re.compile(r"(?<![£$€\d.:])" + _TIME + r"\s*(?:-|–|—|to|until)\s*" + _TIME, re.I)

ADD_BTN = re.compile(r"^\s*\+\s*$|increase|increment|add one|add ticket|plus",
                     re.I)
REMOVE_BTN = re.compile(r"^\s*[-−–]\s*$|decrease|decrement|remove one|minus",
                        re.I)
RESERVE_BTN = re.compile(r"reserve|checkout|check out|continue|get tickets|"
                         r"book|buy|next|proceed", re.I)
RELEASE_BTN = re.compile(r"cancel( reservation| order| booking)?|remove|"
                         r"release|clear basket|empty basket|delete", re.I)
SUCCESS_URL = re.compile(r"checkout|basket|cart|payment|order", re.I)
SUCCESS_TEXT = re.compile(r"reserved|time (left|remaining)|complete your "
                          r"(order|purchase|booking)|pay now|payment details|"
                          r"your basket|order summary", re.I)
FAIL_TEXT = re.compile(r"sold out|no longer available|not available|"
                       r"unavailable|something went wrong|try again|"
                       r"couldn'?t|could not|limit reached|error", re.I)
SOLD_OUT = re.compile(r"sold out|unavailable|not available|off sale", re.I)


def settings(cfg):
    s = dict(DEFAULTS)
    s.update(cfg.get("auto_reserve") or {})
    return s


# ---------------------------------------------------------------- slot logic
def _to_minutes(h, m, ampm, default_pm):
    h, m = int(h), int(m or 0)
    ampm = (ampm or "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and default_pm and 1 <= h < 12:
        h += 12  # "10-10:30" on a night-out event means pm
    return h * 60 + m


def parse_slot(text):
    """Return (start_minutes, end_minutes) for a ticket name, or None."""
    m = SLOT_RE.search(text or "")
    if not m:
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    ap1 = ap1 or ap2  # "10-10:30pm" -> both pm
    start = _to_minutes(h1, m1, ap1, default_pm=True)
    end = _to_minutes(h2, m2, ap2, default_pm=True)
    return start, end


def _hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def order_slots(names, s):
    """Sort ticket names into the order to try them. Returns [(name, start)]."""
    preferred = [_hhmm(t) for t in s["preferred_start_times"]]
    parsed = [(n, parse_slot(n)) for n in names]
    # Anything starting before midday is after midnight -> later in the night.
    parsed = [(n, p[0] + (24 * 60 if p[0] < 12 * 60 else 0))
              for n, p in parsed if p]
    ordered = []
    for want in preferred:
        ordered += [(n, st) for n, st in parsed if st == want]
    if s["then_try_later_slots"] and preferred:
        latest = max(preferred)
        later = sorted([(n, st) for n, st in parsed if st > latest],
                       key=lambda x: x[1])
        ordered += later
    seen, out = set(), []
    for item in ordered:
        if item[0] not in seen:
            seen.add(item[0])
            out.append(item)
    return out


def fmt(minutes):
    h, m = divmod(minutes % (24 * 60), 60)
    suffix = "pm" if h >= 12 else "am"
    return f"{(h - 1) % 12 + 1}:{m:02d}{suffix}"


# ------------------------------------------------------------- page helpers
def _visible_buttons(scope, links=False):
    sel = "button, [role=button]" + (", a[href]" if links else "")
    loc = scope.locator(sel)
    out = []
    for i in range(loc.count()):
        b = loc.nth(i)
        try:
            if b.is_visible():
                out.append(b)
        except Exception:
            pass
    return out


def _btn_label(b):
    try:
        parts = [b.inner_text(timeout=500) or "",
                 b.get_attribute("aria-label") or "",
                 b.get_attribute("title") or "",
                 b.get_attribute("data-testid") or ""]
        return " ".join(p.strip() for p in parts if p).strip()
    except Exception:
        return ""


def _find_button(scope, pattern, exclude=None, links=False):
    for b in _visible_buttons(scope, links):
        label = _btn_label(b)
        if label and pattern.search(label) and not (exclude and
                                                     exclude.search(label)):
            try:
                if b.is_enabled():
                    return b
            except Exception:
                return b
    return None


def _lines(page):
    try:
        text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return []
    return [t.strip() for t in text.splitlines() if t.strip()]


def ticket_names(page):
    """All visible bits of text on the page that look like a time-slot ticket."""
    return list(dict.fromkeys(t for t in _lines(page)
                              if SLOT_RE.search(t) and len(t) < 120))


def ticket_row(page, name):
    """The smallest element containing the ticket name AND a +/- button."""
    label = page.get_by_text(name).first
    node = label
    for _ in range(8):
        node = node.locator("xpath=..")
        adds = _count_buttons(node, ADD_BTN)
        if adds == 1:
            return node
        if adds > 1:
            return None  # reached the whole ticket list -> not a ticket row
    return None


def _count_buttons(scope, pattern):
    return sum(1 for b in _visible_buttons(scope)
               if pattern.search(_btn_label(b)))


def _row_quantity(row):
    """Best guess at the quantity shown in a ticket row (None if unknown)."""
    try:
        for inp in row.locator("input").all():
            v = inp.input_value()
            if v.strip().isdigit():
                return int(v)
        text = row.inner_text()
    except Exception:
        return None
    # A lone number between the -/+ buttons, e.g. "- 1 +".
    m = re.search(r"(?:^|\n)\s*(\d{1,2})\s*(?:\n|$)", text)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------- the reserver
class Reserver:
    """Owns one Chrome window, driven from a single background thread."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.thread = None
        self.ctx = None
        self.pw = None
        self.lock = threading.Lock()

    # -- public API (safe to call from any thread) --
    def reserve(self, event_url, event_label, cfg, notify):
        self._ensure_thread()
        self.jobs.put(("reserve", event_url, event_label, cfg, notify))

    def open_login(self, cfg):
        self._ensure_thread()
        self.jobs.put(("login", cfg))

    # -- worker --
    def _ensure_thread(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._worker,
                                               daemon=True)
                self.thread.start()

    def _worker(self):
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "login":
                    page = self._page(job[1])
                    page.goto(LOGIN_URL)
                    page.bring_to_front()
                    print("[reserve] Log into Fixr in the window that opened. "
                          "You stay logged in for next time.")
                else:
                    self._reserve(*job[1:])
            except Exception as e:
                print(f"[reserve] ! error: {e}")
            finally:
                self.jobs.task_done()

    def _page(self, cfg):
        s = settings(cfg)
        if self.ctx is not None:
            try:
                self.ctx.pages  # still alive?
                return self.ctx.new_page()
            except Exception:
                self.ctx = None
        from playwright.sync_api import sync_playwright
        if self.pw is None:
            self.pw = sync_playwright().start()
        kw = dict(headless=s["headless"], viewport=None)
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel="chrome", **kw)
        except Exception:
            # Chrome not found -> Playwright's bundled Chromium.
            self.ctx = self.pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), **kw)
        pages = self.ctx.pages
        return pages[0] if pages and pages[0].url == "about:blank" \
            else self.ctx.new_page()

    def _reserve(self, url, label, cfg, notify):
        s = settings(cfg)
        page = self._page(cfg)
        page.bring_to_front()
        print(f"[reserve] {label}: opening {url}")

        deadline = time.monotonic() + s["wait_for_tickets_minutes"] * 60
        slots = []
        while True:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            if "login" in page.url.lower() or "sign-in" in page.url.lower():
                print("[reserve] ! Fixr wants you to log in - log in in the "
                      "window, it will keep trying.")
            names = ticket_names(page)
            slots = order_slots(names, s)
            if slots:
                break
            if time.monotonic() > deadline:
                print(f"[reserve] {label}: no matching time-slot tickets "
                      f"found (saw: {names or 'none'}). Giving up.")
                notify(f"Couldn't auto-reserve {label}",
                       "No matching time-slot tickets - check it yourself.",
                       url)
                return
            print(f"[reserve] {label}: no time-slot tickets yet, "
                  "retrying in 5s...")
            time.sleep(5)

        print("[reserve] order to try: " +
              ", ".join(f"{fmt(st)} ({n})" for n, st in slots))
        for name, start in slots:
            result = self._try_slot(page, url, name, s)
            if result == "reserved":
                print(f"[reserve] RESERVED {name} for {label}. "
                      "Pay in the Chrome window!")
                notify(f"Reserved: {label}",
                       f"{name} is in your basket - pay now!", page.url)
                page.bring_to_front()
                return
            if result == "held":
                print("[reserve] ! could not confirm the failed ticket was "
                      "removed - stopping so you don't end up with two. "
                      "Check the Chrome window.")
                notify(f"Check your Fixr basket: {label}",
                       f"Reserving {name} failed and it may still be held.",
                       page.url)
                return
            # "skipped"/"failed" -> next slot
        print(f"[reserve] {label}: could not reserve any slot.")
        notify(f"Couldn't auto-reserve {label}",
               "Every time slot failed - try it yourself.", url)

    def _try_slot(self, page, url, name, s):
        """Returns 'reserved', 'failed' (and released), 'skipped' or 'held'."""
        row = ticket_row(page, name)
        if row is None:
            print(f"[reserve]   {name}: can't find its + button, skipping")
            return "skipped"
        try:
            if SOLD_OUT.search(row.inner_text()):
                print(f"[reserve]   {name}: sold out, skipping")
                return "skipped"
        except Exception:
            pass
        add = _find_button(row, ADD_BTN)
        if add is None:
            print(f"[reserve]   {name}: + button disabled, skipping")
            return "skipped"
        print(f"[reserve]   {name}: adding 1 ticket")
        add.click()
        page.wait_for_timeout(400)

        reserve = _find_button(page, RESERVE_BTN, exclude=ADD_BTN)
        if reserve is None:
            print(f"[reserve]   {name}: no reserve/checkout button found")
            return self._release(page, url, name)
        start_url = page.url
        before = set(_lines(page))
        print(f"[reserve]   {name}: pressing '{_btn_label(reserve)}'")
        reserve.click()

        end = time.monotonic() + s["reserve_timeout_seconds"]
        while time.monotonic() < end:
            page.wait_for_timeout(500)
            # Only judge by text that appeared after pressing the button.
            body = "\n".join(l for l in _lines(page) if l not in before)
            moved = page.url != start_url and SUCCESS_URL.search(page.url)
            if moved or (SUCCESS_TEXT.search(body)
                         and not FAIL_TEXT.search(body)):
                return "reserved"
            if FAIL_TEXT.search(body):
                err = FAIL_TEXT.search(body).group(0)
                print(f"[reserve]   {name}: failed ('{err}')")
                return self._release(page, url, name)
        print(f"[reserve]   {name}: no confirmation after "
              f"{s['reserve_timeout_seconds']}s, treating as failed")
        return self._release(page, url, name)

    def _release(self, page, url, name):
        """Take the ticket back out and confirm it's gone before moving on."""
        # If we ended up on a basket/checkout page, cancel from there.
        if page.url.rstrip("/") != url.rstrip("/"):
            btn = _find_button(page, RELEASE_BTN, links=True)
            if btn is not None:
                print(f"[reserve]   {name}: pressing '{_btn_label(btn)}'")
                page.once("dialog", lambda d: d.accept())
                btn.click()
                page.wait_for_timeout(1500)
                confirm = _find_button(page, re.compile(
                    r"^(yes|confirm|ok|remove|cancel reservation)", re.I))
                if confirm is not None:
                    confirm.click()
                    page.wait_for_timeout(1000)
        # Back on the ticket page, knock the quantity down to 0.
        for _ in range(3):
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            row = ticket_row(page, name)
            if row is None:
                break
            qty = _row_quantity(row)
            if qty == 0 or qty is None and _find_button(row, REMOVE_BTN) is None:
                print(f"[reserve]   {name}: removed (quantity 0)")
                return "failed"
            minus = _find_button(row, REMOVE_BTN)
            if minus is None:
                break
            for _ in range(qty or 1):
                minus.click()
                page.wait_for_timeout(300)
            if _row_quantity(row) in (0, None) and \
                    _find_button(row, REMOVE_BTN) is None:
                print(f"[reserve]   {name}: removed (quantity 0)")
                return "failed"
        return "held"


RESERVER = Reserver()
