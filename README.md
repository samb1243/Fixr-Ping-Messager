# Fixr Ping Messager

Watches one or more web pages and sends a **push notification to your phone**
when the content changes. Tuned for Fixr organiser pages, but works for other
sites too.

## How it works

1. **Fixr organiser pages** are fetched with a fast, lightweight HTTP request.
   The events are server-rendered into the page's `__NEXT_DATA__` JSON, so no
   browser is needed — each check takes ~0.1s.
2. **Other pages** fall back to a real headless browser (Playwright) and watch a
   CSS selector or the whole page text.
3. The result is compared to the last-seen snapshot in `state/`.
4. If it changed, it pushes a notification via [ntfy.sh](https://ntfy.sh) to your
   phone naming the new events, and (if `open_browser_on_change` is on) opens
   **each new event's ticket-selection screen** (`/event/.../tickets`) directly
   in your browser — so you land straight where you pick tickets, not on the
   listing. The phone notification links there too.

## One-time setup

1. Install the **ntfy** app on your phone (iOS / Android) and **subscribe** to
   the topic in `config.json` (`ntfy.topic`).
2. Install dependencies:
   ```
   pip install -r requirements.txt
   python -m playwright install chromium   # only needed for non-Fixr pages
   ```
3. Test notifications:
   ```
   python notify.py
   ```
   Your phone should buzz.

## Usage

**The easy way — the desktop app.** Double-click the **Fixr Ping Messager**
desktop shortcut (or `Fixr Ping Messager.vbs` in this folder). A window opens
with a status light, **Start watching** / **Stop** buttons, the list of pages
being watched, and a live activity feed. Closing the window stops watching.

**Command line** (optional):

- Check once: `python watcher.py`
- Run continuously: `python watcher.py --loop`

The first run of any page saves a baseline and does **not** notify. You only get
pinged on changes *after* that. Only one watcher can run at a time (the app and
CLI share a single-instance guard).

## config.json

```json
{
  "ntfy": { "server": "https://ntfy.sh", "topic": "fixr-watch-50e9aec6" },
  "interval_seconds": 1,
  "open_browser_on_change": true,
  "browsers": ["chrome", "opera gx"],
  "pages": [
    {
      "name": "Timepiece (Fixr organiser)",
      "url": "https://fixr.co/organiser/timepiece?lang=en-US",
      "mode": "fixr_organiser",
      "selector": null,
      "enabled": true
    }
  ]
}
```

- `interval_seconds` — how often the `--loop` checks. **Note:** very low values
  (e.g. `1`) hammer the site and risk being rate-limited or IP-blocked; `3`–`5`
  is plenty for events posted by humans. The loop automatically backs off if the
  site returns "too many requests".
- `open_browser_on_change` — when `true`, a change opens each new Fixr event's
  ticket page in your browser (a new tab each). If many appear at once (>8) it
  opens the listing page instead. Set `false` for phone-only notifications.
- `browsers` — which browsers to open new events in. Defaults to
  `["chrome", "opera gx"]`, so each new event opens in **both** Google Chrome and
  Opera GX at once. `"edge"` is also supported. Any that aren't installed are skipped; if none are
  found it falls back to your default browser. You can also list a full path to
  a browser `.exe`.

## Adding more pages

Add entries to `pages`:

- **Another Fixr organiser** (`/organiser/...`) — copy the timepiece block,
  change `name` + `url`, keep `"mode": "fixr_organiser"`.
- **A Fixr venue** (`/venue/...`) — same, but use `"mode": "fixr_venue"`.
- **A non-Fixr site** — set `"mode": "auto"` and a `"selector"` (a CSS selector
  for the element to watch). Leave `selector` null to watch the whole page text.
  Use `discover.py` to inspect what a page loads:
  ```
  python discover.py "https://example.com/page"
  ```

## Starting and stopping

Open the app (desktop shortcut **Fixr Ping Messager**, or
`Fixr Ping Messager.vbs`) and use the **Start watching** / **Stop** buttons. The
status light is green while watching, red when stopped. Closing the window stops
watching.

It runs **on-demand** — nothing launches at login, and it only runs while the
app (or the CLI loop) is open and you're logged in. For true 24/7 (even when the
PC is off), the same `watcher.py` can run on a small always-on server or free
cloud runner.

If the desktop shortcut is ever lost, recreate one by right-clicking
`Fixr Ping Messager.vbs` → Send to → Desktop (create shortcut).

## Files

| File | Purpose |
|------|---------|
| `app.py` | The desktop app (Start/Stop window) |
| `Fixr Ping Messager.vbs` | Opens the app with no console window |
| `watcher.py` | Watch engine (also runs standalone: `--loop`) |
| `notify.py` | Sends the ntfy push (`python notify.py` self-test) |
| `config.json` | Settings: ntfy topic, interval, list of pages |
| `discover.py` | Inspect what JSON/data a page loads |
| `state/` | Last-seen snapshot per page (auto-created) |
| `watcher.log` | Activity log |
