"""Record how Fixr's ticket reservation works, so the auto-reserve feature can
be built against the real site.

Usage:
    python record_fixr.py "https://fixr.co/event/<some-timepiece-event>/tickets"

A Chrome window opens (it uses its own profile in ./fixr_profile, so you only
need to log in once). Then, by hand:
  1. Log into Fixr if asked.
  2. Add ONE ticket to the basket and reserve it.
  3. Remove it from the basket again (so you don't keep it).
  4. Come back here and press Enter.

Everything the page sent to / received from Fixr is saved to
fixr_recording.json. Passwords, tokens, cookies and personal details
(name, email, phone, card info) are blanked out before saving.
"""
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
PROFILE_DIR = ROOT / "fixr_profile"
OUT_FILE = ROOT / "fixr_recording.json"

# Keys whose values are blanked anywhere they appear.
SECRET_KEY_RE = re.compile(
    r"pass|token|secret|auth|cookie|session|email|phone|mobile|name|"
    r"address|postcode|zip|card|cvc|cvv|iban|dob|birth", re.I)
# ...except these, which describe tickets/events and are needed.
KEEP_KEYS = {"name", "event_name", "ticket_name", "venue_name"}


def redact(obj, key=None):
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if key and key not in KEEP_KEYS and SECRET_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(obj, str):
        obj = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", obj)
    return obj


def parse_body(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text[:4000]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    log = []

    def on_response(response):
        req = response.request
        if req.resource_type not in ("xhr", "fetch", "document"):
            return
        if "fixr" not in req.url:
            return
        entry = {
            "method": req.method,
            "url": req.url,
            "status": response.status,
            "request_body": parse_body(req.post_data),
        }
        ctype = (response.headers or {}).get("content-type", "")
        if "json" in ctype:
            try:
                entry["response_body"] = response.json()
            except Exception:
                pass
        log.append(entry)
        print(f"  {req.method} {response.status} {req.url[:110]}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False,
            viewport=None)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ctx.on("response", on_response)
        page.goto(url)
        print("\nChrome is open. Log in, add ONE ticket, reserve it, then "
              "remove it from the basket.")
        input("When you're done, press Enter here... ")
        # Save what the ticket page looks like (buttons, labels) too.
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
            html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S)
            html = re.sub(r"<svg\b.*?</svg>", "<svg/>", html, flags=re.S)
            html = redact(html)
        except Exception as e:
            html = f"(could not capture page: {e})"
        ctx.close()

    OUT_FILE.write_text(json.dumps(
        {"start_url": url, "requests": redact(log), "ticket_page_html": html},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(log)} requests to {OUT_FILE}")
    print("Send that file to Claude (attach it in the chat).")


if __name__ == "__main__":
    main()
