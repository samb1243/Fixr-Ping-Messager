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
  "browsers": ["chrome"],
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
  `["chrome"]`. You can list more to open each event in several at once
  (`"edge"` and `"opera gx"` are also supported). Any that aren't installed are skipped; if none are
  found it falls back to your default browser. You can also list a full path to
  a browser `.exe`.

  To check it without waiting for a new event, click **Test browsers** in the
  app (or run `python watcher.py --test-browsers`). It opens fixr.co in every
  listed browser and shows where it found each one in the activity feed.

## Auto-reserve (Timepiece)

Pages with `"auto_reserve": true` (only Timepiece at the moment) don't just
open the new event: a small Chrome extension puts **one ticket in your basket**
for you, in a normal tab of your everyday Chrome (so it uses your normal Fixr
login). You then pay in that tab yourself.

1. **Install the extension once in Chrome:** go to `chrome://extensions`, turn
   on **Developer mode** (top right), click **Load unpacked** and pick the
   `chrome_extension` folder inside this project. "Fixr Auto-Reserve" appears
   in the list. After a `git pull` that changes it, click its reload ↻ icon.
   (Other Chromium browsers listed in `browsers`, e.g. `"edge"`, need it too.)
2. Be logged into Fixr in Chrome as normal.
3. When a new Timepiece event appears, the app opens its ticket page in every
   browser at once, and in each one the extension independently tries one ticket per time slot in this order:
   10:00–10:30pm, 9:30pm, 9:00pm, 8:30pm, 8:00–8:30pm, then later slots
   (10:30pm, 11pm, … past midnight). Slots before 8pm are never tried.
4. If a slot fails, it removes that ticket and checks it's gone before trying
   the next one. If it can't confirm the removal, it stops and pings you.
5. When one is reserved you get an urgent phone ping saying which browser has
   it, and that tab stays on the basket. Both browsers can end up holding a
   ticket if you list more than one: pay in one and press **Cancel** in the
   other. **Pay there before the basket timer runs out.**

**Several Fixr accounts (one ticket each):** each account lives in its own
Chrome profile (the profile picture at the top right of Chrome), which keeps
its own Fixr login. List them in `config.json`:

```json
"accounts": [
  { "name": "Account 1", "chrome_profile": "Default" },
  { "name": "Account 2", "chrome_profile": "Profile 1" }
]
```

- `name` is just a label for the activity feed and phone pings.
- `chrome_profile` is the profile's folder: open `chrome://version` in that
  profile and copy the last part of **Profile Path** (e.g. `Default`,
  `Profile 1`).
- In **each** profile: install the extension (Load unpacked, as above) and log
  into that account's Fixr.
- Add more accounts by adding more lines. Remove the `accounts` block to go
  back to a single browser.

When a new event appears, each account gets its own Chrome window and tries the
same slot order independently. Feed lines and pings say which account, e.g.
`(Account 2) RESERVED …` - pay in that account's window before its timer runs
out. **Log in to Fixr** opens the login page in every account's profile, and
**Test browsers** shows whether each profile was found.

**Turning it on/off:** tick or untick **Auto-reserve tickets** in the app. It
takes effect straight away, even while watching. When it's off, new events just
open in your browser as before.

Each account also has its own tick box on the **Accounts:** row, which picks
which accounts' Chrome windows open for a new event:

- **Auto-reserve ticked:** each ticked account opens the event and reserves.
- **Auto-reserve unticked:** each ticked account just opens the event's ticket
  page (nothing is reserved), so you can go in by hand on whichever accounts
  you want.

If every account is unticked, new events open in your normal Chrome instead.
**Log in to Fixr** always opens every account.

If tickets aren't on sale yet it keeps reloading for `wait_for_tickets_minutes`.
Every step shows in the app's activity feed as `[reserve] …` lines. The
button/label matching is a best guess at Fixr's page, so if it does the wrong
thing, send those lines so it can be fixed.

**Testing it:** click **Test auto-reserve** in the app (or run
`python watcher.py --test-reserve "Thursday Indie Night"`). It finds that event
on the Timepiece page, sends the phone ping and runs auto-reserve on it as if it
had just been posted. This puts a real ticket in your basket; if you don't pay,
it's released when the basket timer runs out. It opens in your normal Chrome,
so the extension must be installed.

Settings (`auto_reserve` in `config.json`): `preferred_start_times` (24h, in the
order to try), `then_try_later_slots`, `wait_for_tickets_minutes`,
`reserve_timeout_seconds`, `enabled` to switch it all off, and
`use_own_window: true` to go back to the old separate app-controlled Chrome
window instead of the extension.

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
| `chrome_extension/` | The Fixr Auto-Reserve Chrome extension (does the reserving) |
| `reserve.py` | Starts auto-reserve; shows the extension's progress in the app |
| `record_fixr.py` | Records Fixr's requests while you reserve by hand (debugging) |
| `discover.py` | Inspect what JSON/data a page loads |
| `state/` | Last-seen snapshot per page (auto-created) |
| `watcher.log` | Activity log |
