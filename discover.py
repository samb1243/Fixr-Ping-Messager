"""Discovery helper: load a page in a real browser and dump every JSON
response it fetches, so we can find the internal API that holds the data
we want to monitor.

Usage:
    python discover.py                      # uses the first page in config.json
    python discover.py "https://some/url"   # or pass a URL directly

JSON responses are saved to ./discovery/ and a summary is printed.
"""
import json
import pathlib
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

OUT_DIR = pathlib.Path("discovery")


def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        cfg = json.loads(pathlib.Path("config.json").read_text(encoding="utf-8"))
        url = cfg["pages"][0]["url"]

    OUT_DIR.mkdir(exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()

    captured = []

    def on_response(response):
        ctype = (response.headers or {}).get("content-type", "")
        if "application/json" not in ctype:
            return
        try:
            body = response.json()
        except Exception:
            return
        captured.append((response.url, body))

    print(f"Loading {url} ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=60000)
        # Give late XHRs a moment.
        page.wait_for_timeout(3000)
        browser.close()

    if not captured:
        print("No JSON responses captured. The data may be server-rendered, "
              "or loaded differently. We'll fall back to DOM scraping.")
        return

    print(f"\nCaptured {len(captured)} JSON response(s):\n")
    for i, (resp_url, body) in enumerate(captured):
        path = urlparse(resp_url).path
        # A short fingerprint of what's inside, to spot the events feed.
        keys = list(body.keys()) if isinstance(body, dict) else f"list[{len(body)}]"
        fname = OUT_DIR / f"{i:02d}.json"
        fname.write_text(
            json.dumps({"url": resp_url, "body": body}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[{i:02d}] {path}")
        print(f"     top-level: {keys}")
        print(f"     saved -> {fname}")
    print(f"\nOpen the files in ./discovery/ and find the one that lists the "
          f"organiser's events. Then put a unique part of its URL into "
          f"'api_url_contains' in config.json.")


if __name__ == "__main__":
    main()
