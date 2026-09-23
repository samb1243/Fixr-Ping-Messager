// Fixr Auto-Reserve: runs on fixr.co pages in your normal Chrome.
//
// Fixr Ping Messager opens a new event's ticket page with
// "#fixr-autoreserve=<settings>" on the end. That starts a run: one ticket
// per time slot, in the configured order, until one reserves. A failed
// ticket is taken back out (quantity back to 0) before the next slot is
// tried. Progress is kept in extension storage so it survives page loads.
//
// The page matching is a best guess at Fixr's ticket page (see reserve.py,
// which has the same rules) -- every step is logged to the app's activity
// feed so it can be fixed up against the real site.
(() => {
  const KEY = "fixrAutoReserve";
  const TAG = "fixr-autoreserve=";

  const TIME = String.raw`(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?`;
  const SLOT_RE = new RegExp(
    String.raw`(?<![£$€\d.:])` + TIME + String.raw`\s*(?:-|–|—|to|until)\s*` + TIME, "i");
  const ADD_BTN = /^\s*\+\s*$|increase|increment|add one|add ticket|plus/i;
  const REMOVE_BTN = /^\s*[-−–]\s*$|decrease|decrement|remove one|minus/i;
  const RESERVE_BTN = /reserve|checkout|check out|continue|get tickets|book|buy|next|proceed/i;
  const SUCCESS_URL = /checkout|basket|cart|payment|order/i;
  const SUCCESS_TEXT = /reserved|time (left|remaining)|complete your (order|purchase|booking)|pay now|payment details|your basket|order summary/i;
  const FAIL_TEXT = /sold out|no longer available|not available|unavailable|something went wrong|try again|couldn'?t|could not|limit reached|error/i;
  const SOLD_OUT = /sold out|unavailable|not available|off sale/i;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const getState = async () => (await chrome.storage.local.get(KEY))[KEY];
  const setState = (s) => chrome.storage.local.set({ [KEY]: s });
  const path = (u) => new URL(u, location.href).pathname.replace(/\/$/, "");

  function send(msg) {
    try { chrome.runtime.sendMessage(msg); } catch (e) { /* extension reloaded */ }
  }
  function log(s, text) {
    console.log("[fixr auto-reserve]", text);
    send({ type: "log", port: s.cfg.log_port, text });
  }
  function push(s, title, message, url) {
    send({ type: "push", ntfy: s.cfg.ntfy, title, message, url });
  }

  // ------------------------------------------------------------ slot logic
  function toMinutes(h, m, ampm) {
    h = +h; m = +(m || 0); ampm = (ampm || "").toLowerCase();
    if (ampm === "pm" && h < 12) h += 12;
    else if (ampm === "am" && h === 12) h = 0;
    else if (!ampm && h >= 1 && h < 12) h += 12; // "10-10:30" at a night out = pm
    return h * 60 + m;
  }
  function slotStart(text) {
    const m = SLOT_RE.exec(text || "");
    if (!m) return null;
    const start = toMinutes(m[1], m[2], m[3] || m[6]);
    return start < 12 * 60 ? start + 24 * 60 : start; // after midnight = later
  }
  function orderSlots(names, cfg) {
    const pref = cfg.preferred_start_times.map((t) => {
      const [h, m] = t.split(":"); return +h * 60 + +m;
    });
    const parsed = names.map((n) => ({ name: n, start: slotStart(n) }))
      .filter((x) => x.start !== null);
    let out = [];
    for (const want of pref) out.push(...parsed.filter((x) => x.start === want));
    if (cfg.then_try_later_slots && pref.length) {
      const latest = Math.max(...pref);
      out.push(...parsed.filter((x) => x.start > latest).sort((a, b) => a.start - b.start));
    }
    const seen = new Set();
    return out.filter((x) => !seen.has(x.name) && seen.add(x.name));
  }
  function fmt(min) {
    const h = Math.floor(min / 60) % 24, m = min % 60;
    return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")}${h >= 12 ? "pm" : "am"}`;
  }

  // ---------------------------------------------------------- page helpers
  const lines = () => (document.body ? document.body.innerText : "")
    .split("\n").map((t) => t.trim()).filter(Boolean);
  const visible = (el) => el.getClientRects().length > 0;
  const enabled = (el) => !el.disabled && el.getAttribute("aria-disabled") !== "true";
  function label(el) {
    return [el.innerText, el.getAttribute("aria-label"), el.getAttribute("title"),
      el.getAttribute("data-testid")].filter(Boolean).join(" ").trim();
  }
  function buttons(scope, pattern) {
    return [...scope.querySelectorAll("button, [role=button]")]
      .filter((b) => visible(b) && pattern.test(label(b)));
  }
  function findButton(scope, pattern, exclude) {
    return buttons(scope, pattern)
      .find((b) => enabled(b) && !(exclude && exclude.test(label(b)))) || null;
  }
  function ticketNames() {
    return [...new Set(lines().filter((t) => SLOT_RE.test(t) && t.length < 120))];
  }
  function ticketRow(name) {
    // Deepest element whose text contains the ticket name...
    let el = [...document.body.querySelectorAll("*")]
      .filter((e) => e.innerText && e.innerText.includes(name))
      .find((e) => ![...e.children].some((c) => c.innerText && c.innerText.includes(name)));
    // ...then climb until it holds exactly one "+" button (more = whole list).
    for (let i = 0; el && i < 8; i++) {
      el = el.parentElement;
      if (!el) return null;
      const adds = buttons(el, ADD_BTN).length;
      if (adds === 1) return el;
      if (adds > 1) return null;
    }
    return null;
  }
  function rowQuantity(row) {
    for (const inp of row.querySelectorAll("input")) {
      if (/^\d+$/.test(inp.value.trim())) return +inp.value;
    }
    const m = /(?:^|\n)\s*(\d{1,2})\s*(?:\n|$)/.exec(row.innerText);
    return m ? +m[1] : null;
  }

  // ------------------------------------------------------------- the run
  async function finish(s, title, message, url) {
    s.active = false;
    await setState(s);
    push(s, title, message, url);
  }

  async function success(s) {
    log(s, `RESERVED ${s.current} for ${s.cfg.label}. Pay in this Chrome tab!`);
    await finish(s, `Reserved: ${s.cfg.label}`, `${s.current} is in your basket - pay now!`,
      location.href);
  }

  // Take the ticket back out and check it's gone. Returns true if confirmed.
  async function release(s, name) {
    for (let attempt = 0; attempt < 5; attempt++) {
      const row = ticketRow(name);
      if (!row) break;
      const qty = rowQuantity(row);
      const minus = findButton(row, REMOVE_BTN);
      if (qty === 0 || (qty === null && !minus)) {
        log(s, `  ${name}: removed (quantity 0)`);
        return true;
      }
      if (!minus) break;
      minus.click();
      await sleep(400);
    }
    return false;
  }

  async function failed(s, name) {
    if (!(await release(s, name))) {
      log(s, `! could not confirm ${name} was removed - stopping so you don't end up ` +
        "with two. Check your basket.");
      await finish(s, `Check your Fixr basket: ${s.cfg.label}`,
        `Reserving ${name} failed and it may still be held.`, location.href);
      return false;
    }
    s.tried.push(name);
    s.phase = "find";
    s.current = null;
    await setState(s);
    return true;
  }

  async function trySlot(s, name) {
    const row = ticketRow(name);
    if (!row) { log(s, `  ${name}: can't find its + button, skipping`); return "skip"; }
    if (SOLD_OUT.test(row.innerText)) { log(s, `  ${name}: sold out, skipping`); return "skip"; }
    const add = findButton(row, ADD_BTN);
    if (!add) { log(s, `  ${name}: + button disabled, skipping`); return "skip"; }
    log(s, `  ${name}: adding 1 ticket`);
    add.click();
    await sleep(400);

    const reserve = findButton(document, RESERVE_BTN, ADD_BTN);
    if (!reserve) {
      log(s, `  ${name}: no reserve/checkout button found`);
      return (await failed(s, name)) ? "next" : "stop";
    }
    // Watch everything the page adds/changes after pressing reserve, so a
    // repeat of an earlier error message still counts.
    let fresh = "";
    const obs = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === "characterData") fresh += "\n" + m.target.textContent;
        for (const n of m.addedNodes) {
          fresh += "\n" + (n.innerText !== undefined ? n.innerText : n.textContent || "");
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    const startPath = location.pathname;
    s.phase = "reserving";
    s.current = name;
    await setState(s);
    log(s, `  ${name}: pressing '${label(reserve)}'`);
    reserve.click();

    const end = Date.now() + s.cfg.reserve_timeout_seconds * 1000;
    while (Date.now() < end) {
      await sleep(500);
      if ((location.pathname !== startPath && SUCCESS_URL.test(location.pathname)) ||
          (SUCCESS_TEXT.test(fresh) && !FAIL_TEXT.test(fresh))) {
        obs.disconnect();
        await success(s);
        return "stop";
      }
      const err = FAIL_TEXT.exec(fresh);
      if (err) {
        obs.disconnect();
        log(s, `  ${name}: failed ('${err[0]}')`);
        return (await failed(s, name)) ? "next" : "stop";
      }
    }
    obs.disconnect();
    log(s, `  ${name}: no confirmation after ${s.cfg.reserve_timeout_seconds}s, treating as failed`);
    return (await failed(s, name)) ? "next" : "stop";
  }

  async function runTicketPage(s) {
    // Wait for the ticket list to render (and for tickets to go on sale).
    let slots = [];
    for (let i = 0; i < 16; i++) {
      slots = orderSlots(ticketNames(), s.cfg);
      if (slots.length) break;
      await sleep(500);
    }
    if (!slots.length) {
      if (Date.now() > s.started + s.cfg.wait_for_tickets_minutes * 60000) {
        log(s, `${s.cfg.label}: no matching time-slot tickets found. Giving up.`);
        await finish(s, `Couldn't auto-reserve ${s.cfg.label}`,
          "No matching time-slot tickets - check it yourself.", s.url);
      } else {
        log(s, `${s.cfg.label}: no time-slot tickets yet, reloading...`);
        await sleep(4000);
        location.reload();
      }
      return;
    }
    if (!s.tried.length) {
      log(s, "order to try: " + slots.map((x) => `${fmt(x.start)} (${x.name})`).join(", "));
    }
    for (const { name } of slots) {
      if (s.tried.includes(name)) continue;
      const r = await trySlot(s, name);
      if (r === "stop") return;
      if (r === "skip") { s.tried.push(name); await setState(s); }
    }
    log(s, `${s.cfg.label}: could not reserve any slot.`);
    await finish(s, `Couldn't auto-reserve ${s.cfg.label}`,
      "Every time slot failed - try it yourself.", s.url);
  }

  async function main() {
    const s = await getState();
    if (!s || !s.active) return;
    // Forget runs that are long dead (e.g. Chrome was closed mid-run).
    if (Date.now() - s.started > (s.cfg.wait_for_tickets_minutes + 15) * 60000) {
      s.active = false;
      await setState(s);
      return;
    }
    const onTicketPage = path(location.href) === path(s.url);
    if (s.phase === "reserving") {
      if (!onTicketPage) {
        // Pressing reserve took us to a new page: basket/checkout = success.
        await sleep(1500);
        if (SUCCESS_URL.test(location.pathname) || SUCCESS_TEXT.test(lines().join("\n"))) {
          await success(s);
        } else {
          log(s, `pressed reserve and landed on ${location.href} - not sure if that ` +
            "worked. If Fixr wants you to log in, do that; then check your basket.");
        }
        return;
      }
      // Reloaded back onto the ticket page mid-reserve -> that one failed.
      log(s, `  ${s.current}: back on the ticket page, treating as failed`);
      if (!(await failed(s, s.current))) return;
    }
    if (onTicketPage) await runTicketPage(s);
  }

  async function boot() {
    const i = location.hash.indexOf(TAG);
    if (i !== -1) {
      // A fresh run started by the app.
      const b64 = location.hash.slice(i + TAG.length).replace(/-/g, "+").replace(/_/g, "/");
      const cfg = JSON.parse(new TextDecoder().decode(
        Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))));
      const url = location.href.split("#")[0];
      history.replaceState(null, "", url);
      const s = { active: true, url, cfg, tried: [], phase: "find", current: null,
        started: Date.now() };
      await setState(s);
      log(s, `${cfg.label}: extension started in Chrome on ${url}`);
    }
    if (document.readyState === "loading") {
      await new Promise((r) => document.addEventListener("DOMContentLoaded", r, { once: true }));
    }
    await main();
  }

  boot().catch((e) => console.error("[fixr auto-reserve]", e));
})();
