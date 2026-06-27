"""Scrape WhatsApp Web for groups whose title matches the configured keywords.

Design notes
------------
* Uses a *persistent* browser profile so the QR code only has to be scanned once.
* WhatsApp Web virtualises the message list (off-screen rows are removed from the
  DOM), so messages are extracted **incrementally** after every scroll step and
  merged by a stable key. Image bubbles are converted to data-URLs at capture
  time because blob: URLs expire once a row is recycled.
* Selectors lean on the long-stable `data-pre-plain-text` attribute (which holds
  "[time, date] sender:") and fall back gracefully.

Run:  python src/scrape_whatsapp.py            # scrape all matching groups
      python src/scrape_whatsapp.py --inspect  # dump page for selector debugging
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import random
import re
import time
from pathlib import Path

from dateutil import parser as dtparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from common import LOG, ROOT, date_window, load_config, p, read_json, slug, write_json

WA_URL = "https://web.whatsapp.com/"


# ── gentle mode (human-like pacing to lower ban risk; always read-only) ──────
def _gentle(cfg: dict) -> dict:
    s = cfg.get("scrape", {})
    g = s.get("gentle", {}) or {}
    return {
        "on": bool(s.get("gentle_mode", True)),
        "amin": int(g.get("min_action_delay_ms", 900)),
        "amax": int(g.get("max_action_delay_ms", 2300)),
        "gmin": int(g.get("min_group_pause_s", 5)),
        "gmax": int(g.get("max_group_pause_s", 18)),
        "cap": int(g.get("groups_per_run", 0) or 0),
    }


def _action_pause(page, cfg: dict, base_ms: int) -> None:
    """Wait between scroll/extract steps: jittered when gentle, else fixed."""
    g = _gentle(cfg)
    if g["on"]:
        page.wait_for_timeout(random.randint(g["amin"], g["amax"]))
    else:
        page.wait_for_timeout(base_ms)


def _group_pause(cfg: dict) -> None:
    g = _gentle(cfg)
    if g["on"]:
        secs = random.randint(g["gmin"], g["gmax"])
        LOG.info("    (gentle) pausing %ds before next group...", secs)
        time.sleep(secs)

# JavaScript run inside the page to pull the currently-rendered messages.
# Returns {messages:[{key,pretext,text,images:[dataURL]}], scrollTop, scrollHeight, clientHeight}
EXTRACT_JS = r"""
async () => {
  const main = document.querySelector('#main');
  if (!main) return {messages: [], scrollTop: 0, scrollHeight: 0, clientHeight: 0};

  // locate the scrollable message pane
  let pane = null;
  const candidates = main.querySelectorAll('div');
  for (const d of candidates) {
    if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > 200) { pane = d; break; }
  }

  const blobToDataURL = (url) => new Promise(async (resolve) => {
    try {
      const resp = await fetch(url);
      const blob = await resp.blob();
      const r = new FileReader();
      r.onloadend = () => resolve(r.result);
      r.onerror = () => resolve(null);
      r.readAsDataURL(blob);
    } catch (e) { resolve(null); }
  });

  const rows = main.querySelectorAll('div[role="row"]');
  const out = [];
  for (const row of rows) {
    const ct = row.querySelector('[data-pre-plain-text]');
    const pretext = ct ? ct.getAttribute('data-pre-plain-text') : '';
    // text — robust across builds: the copyable-text (data-pre-plain-text) element's
    // innerText IS the message body; fall back to common text-span selectors.
    let text = '';
    if (ct) text = (ct.innerText || '').trim();
    if (!text) {
      const spans = row.querySelectorAll('span.selectable-text, span[dir="ltr"], span[dir="auto"]');
      if (spans.length) text = Array.from(spans).map(s => s.innerText).join(' ').trim();
    }
    // images (photos/posters) — skip tiny sticker/emoji/avatar imgs
    const images = [];
    for (const img of row.querySelectorAll('img')) {
      const w = img.naturalWidth || img.width || 0;
      const h = img.naturalHeight || img.height || 0;
      const src = img.getAttribute('src') || '';
      if ((w >= 90 && h >= 90) && (src.startsWith('blob:') || src.startsWith('data:') || src.startsWith('http'))) {
        const durl = src.startsWith('data:') ? src : await blobToDataURL(src);
        if (durl) images.push(durl);
      }
    }
    if (!pretext && !text && images.length === 0) continue;
    const key = (pretext || '') + '||' + (text || '').slice(0, 120) + '||' + images.length;
    out.push({key, pretext, text, images});
  }
  return {
    messages: out,
    scrollTop: pane ? pane.scrollTop : 0,
    scrollHeight: pane ? pane.scrollHeight : 0,
    clientHeight: pane ? pane.clientHeight : 0,
  };
}
"""

SCROLL_UP_JS = r"""
() => {
  const main = document.querySelector('#main');
  if (!main) return;
  let pane = null;
  for (const d of main.querySelectorAll('div')) {
    if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > 200) { pane = d; break; }
  }
  if (pane) pane.scrollTop = Math.max(0, pane.scrollTop - pane.clientHeight * 0.85);
}
"""

PRETEXT_RE = re.compile(r"\[(?P<time>[^,\]]+),\s*(?P<date>[^\]]+)\]\s*(?P<sender>.*?):\s*$")


def parse_pretext(pretext: str):
    """Return (sender, datetime) from '[10:30, 16/05/2026] Name:' — tolerant of locale."""
    if not pretext:
        return None, None
    m = PRETEXT_RE.match(pretext.strip())
    if not m:
        return None, None
    sender = m.group("sender").strip()
    raw = f"{m.group('date').strip()} {m.group('time').strip()}"
    when = None
    for dayfirst in (True, False):
        try:
            cand = dtparse.parse(raw, dayfirst=dayfirst)
            when = cand
            if cand.date() <= dt.date.today():
                break
        except Exception:
            continue
    return sender, when


# Read the (virtualised) chat-list directly — no dependency on the search box.
# IMPORTANT: the scrollable element is usually a *descendant* of #pane-side, not
# #pane-side itself, so we locate the real scroller every call.
CHATLIST_JS = r"""
() => {
  const pane = document.querySelector('#pane-side')
            || document.querySelector('div[aria-label="Chat list"]')
            || document.querySelector('[aria-label="Chat list"]');
  if (!pane) return {ok:false, reason:'no-pane', titles:[], counts:{}, atBottom:true};
  // find the actual scrollable container
  let scroller = pane;
  if (!(pane.scrollHeight > pane.clientHeight + 20)) {
    for (const d of pane.querySelectorAll('div')) {
      if (d.scrollHeight > d.clientHeight + 20 && d.clientHeight > 150) { scroller = d; break; }
    }
  }
  // pick whichever role yields rows
  let rowsel = 'listitem';
  let rows = pane.querySelectorAll('[role="listitem"]');
  if (!rows.length) { rows = pane.querySelectorAll('[role="row"]'); rowsel = 'row'; }
  if (!rows.length) { rows = pane.querySelectorAll('[role="gridcell"]'); rowsel = 'gridcell'; }
  const titles = [];
  rows.forEach(r => {
    let name = null;
    const t = r.querySelector('span[title]');
    if (t && t.getAttribute('title')) name = t.getAttribute('title');
    if (!name) { const s = r.querySelector('span[dir="auto"]'); if (s) name = (s.innerText||'').split('\n')[0]; }
    if (name && name.trim()) titles.push(name.trim());
  });
  const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 5;
  return {ok:true, rowsel, titles, atBottom,
          counts:{listitem: pane.querySelectorAll('[role="listitem"]').length,
                  row: pane.querySelectorAll('[role="row"]').length,
                  gridcell: pane.querySelectorAll('[role="gridcell"]').length,
                  spanTitle: pane.querySelectorAll('span[title]').length},
          scrollTop: scroller.scrollTop, scrollHeight: scroller.scrollHeight,
          clientHeight: scroller.clientHeight};
}
"""

# frac == 0 -> jump to top; otherwise scroll by a fraction of the viewport.
SCROLL_CHATLIST_JS = r"""
(frac) => {
  const pane = document.querySelector('#pane-side')
            || document.querySelector('[aria-label="Chat list"]');
  if (!pane) return;
  let scroller = pane;
  if (!(pane.scrollHeight > pane.clientHeight + 20)) {
    for (const d of pane.querySelectorAll('div')) {
      if (d.scrollHeight > d.clientHeight + 20 && d.clientHeight > 150) { scroller = d; break; }
    }
  }
  if (frac === 0) scroller.scrollTop = 0;
  else scroller.scrollBy(0, scroller.clientHeight * frac);
}
"""


def wait_for_chat_list(page, timeout: int = 40):
    """Poll until the chat list actually has rows (it loads after login)."""
    data = {}
    for i in range(timeout):
        data = page.evaluate(CHATLIST_JS)
        if data.get("ok") and data.get("titles"):
            LOG.info("Chat list ready: %d visible rows (role=%s)", len(data["titles"]), data["rowsel"])
            return data
        page.wait_for_timeout(1000)
    LOG.warning("Chat list did not populate within %ds. Diagnostics: %s",
                timeout, data.get("counts"))
    return data


def discover_groups(page, keywords) -> list[str]:
    """Scroll the chat list top-to-bottom, clicking nothing yet, just collecting
    every title that matches a keyword."""
    wait_for_chat_list(page)
    found: dict[str, None] = {}
    seen_all: set[str] = set()
    page.evaluate(SCROLL_CHATLIST_JS, 0)      # jump to top
    page.wait_for_timeout(500)
    stale = 0
    for r in range(400):
        data = page.evaluate(CHATLIST_JS)
        for t in data.get("titles", []):
            seen_all.add(t)
            if any(k.lower() in t.lower() for k in keywords):
                found.setdefault(t, None)
        if data.get("atBottom"):
            break
        before = len(seen_all)
        page.evaluate(SCROLL_CHATLIST_JS, 0.85)
        page.wait_for_timeout(400)
        stale = stale + 1 if len(seen_all) == before else 0
        if stale >= 6:
            break
    LOG.info("  scanned %d chats in list; %d match keyword(s): %s",
             len(seen_all), len(found), list(found.keys()))
    return list(found.keys())


# Full pointer/mouse event sequence — WhatsApp's chat-list selection reacts to
# pointerdown/mousedown, which a plain element.click() does NOT fire.
JS_REAL_CLICK = r"""
(title) => {
  const pane = document.querySelector('#pane-side')
            || document.querySelector('[aria-label="Chat list"]');
  if (!pane) return false;
  let rows = pane.querySelectorAll('[role="row"]');
  if (!rows.length) rows = pane.querySelectorAll('[role="listitem"]');
  if (!rows.length) rows = pane.querySelectorAll('[role="gridcell"]');
  for (const r of rows) {
    const t = r.querySelector('span[title]');
    let name = t ? t.getAttribute('title') : null;
    if (!name) { const s = r.querySelector('span[dir="auto"]'); if (s) name = (s.innerText||'').split('\n')[0]; }
    if (name && name.trim() === title) {
      const el = r.querySelector('div[role="gridcell"]') || t || r;
      el.scrollIntoView({block:'center'});
      const rect = el.getBoundingClientRect();
      const o = {bubbles:true, cancelable:true, view:window,
                 clientX:rect.left+rect.width/2, clientY:rect.top+rect.height/2};
      for (const type of ['pointerover','pointerenter','pointerdown','mousedown',
                          'pointerup','mouseup','click']) {
        const E = type.startsWith('pointer') ? PointerEvent : MouseEvent;
        el.dispatchEvent(new E(type, o));
      }
      return true;
    }
  }
  return false;
}
"""


def open_group(page, title: str) -> bool:
    """Scroll the chat list to find the matching row, then open it with a REAL
    mouse click (Playwright native click; JS pointer-event dispatch as fallback)."""
    page.evaluate(SCROLL_CHATLIST_JS, 0)
    page.wait_for_timeout(400)
    pane = page.locator("#pane-side")
    for r in range(400):
        loc = pane.get_by_title(title, exact=True)
        if loc.count() > 0:
            opened = False
            try:
                loc.first.scroll_into_view_if_needed(timeout=3000)
                loc.first.click(timeout=5000)
                opened = True
            except Exception as e:
                LOG.info("    native click failed (%s); using JS event dispatch", str(e)[:60])
                opened = page.evaluate(JS_REAL_CLICK, title)
            if opened:
                try:
                    page.wait_for_selector("#main", timeout=12000)
                    page.wait_for_timeout(1200)
                    return True
                except PWTimeout:
                    LOG.info("    opened but #main not detected; retrying via JS click")
                    if page.evaluate(JS_REAL_CLICK, title):
                        try:
                            page.wait_for_selector("#main", timeout=8000)
                            page.wait_for_timeout(1000)
                            return True
                        except PWTimeout:
                            return False
                    return False
        data = page.evaluate(CHATLIST_JS)
        if data.get("atBottom"):
            return False
        page.evaluate(SCROLL_CHATLIST_JS, 0.85)
        page.wait_for_timeout(350)
    return False


def scrape_group(page, title: str, cutoff: dt.date, cfg: dict) -> list[dict]:
    collected: dict[str, dict] = {}
    rounds = cfg["scrape"]["max_scroll_rounds"]
    pause = cfg["scrape"]["scroll_pause_ms"]
    cap = cfg["scrape"]["per_group_message_cap"]
    stale = 0
    reached_cutoff = False

    for r in range(rounds):
        data = page.evaluate(EXTRACT_JS)
        new = 0
        for m in data["messages"]:
            if m["key"] not in collected:
                collected[m["key"]] = m
                new += 1
        # find oldest dated message so far
        oldest = None
        for m in collected.values():
            _, when = parse_pretext(m["pretext"])
            if when and (oldest is None or when < oldest):
                oldest = when
        if oldest and oldest.date() < cutoff:
            reached_cutoff = True
            LOG.info("    reached cutoff (oldest=%s) after %d rounds", oldest.date(), r + 1)
            break
        if len(collected) >= cap:
            LOG.info("    hit per-group cap (%d)", cap)
            break
        stale = stale + 1 if new == 0 else 0
        if r % 5 == 0:
            LOG.info("    ...scrolling up: %d messages so far%s",
                     len(collected),
                     f", oldest={oldest.date()}" if oldest else "")
        if stale >= 4 and data["scrollTop"] <= 1:
            LOG.info("    top of history reached after %d rounds", r + 1)
            break
        page.evaluate(SCROLL_UP_JS)
        _action_pause(page, cfg, pause)

    if not reached_cutoff:
        LOG.info("    collected %d rendered messages (did not pass cutoff)", len(collected))
    return list(collected.values())


def materialise(group: str, raw_msgs: list[dict], cutoff: dt.date, cfg: dict) -> dict:
    media_dir = p(cfg, "media")
    gslug = slug(group)
    messages = []
    img_n = 0
    for m in raw_msgs:
        sender, when = parse_pretext(m["pretext"])
        if when and when.date() < cutoff:
            continue
        media_files = []
        for durl in m.get("images", []):
            try:
                header, b64 = durl.split(",", 1)
                ext = "png" if "png" in header else "jpg"
                fn = f"{gslug}_{img_n:04d}.{ext}"
                (media_dir / fn).write_bytes(base64.b64decode(b64))
                media_files.append(str((Path(cfg["paths"]["media"]) / fn).as_posix()))
                img_n += 1
            except Exception as e:
                LOG.warning("    image save failed: %s", e)
        messages.append({
            "group": group,
            "sender": sender or "Unknown",
            "timestamp": when.isoformat() if when else None,
            "text": m.get("text", "").strip(),
            "media": media_files,
            "has_image": bool(media_files),
        })
    # chronological order
    messages.sort(key=lambda x: x["timestamp"] or "")
    return {"group": group, "scraped_at": dt.datetime.now().isoformat(),
            "message_count": len(messages), "messages": messages}


def wait_for_login(page) -> None:
    LOG.info("Waiting for WhatsApp Web login (scan the QR code if prompted)...")
    for _ in range(180):  # up to ~3 min
        if page.query_selector("#pane-side") or page.query_selector('div[aria-label="Chat list"]'):
            LOG.info("Logged in.")
            page.wait_for_timeout(1500)
            return
        time.sleep(1)
    raise RuntimeError("Login timed out — QR was not scanned.")


def run(cfg: dict, inspect: bool = False) -> list[dict]:
    """Scrape according to `cfg`. Returns the per-group index.

    Group selection: chats whose title matches `cfg['keywords']`, PLUS any exact
    titles listed in `cfg['target_chats']` (groups *or* one-to-one chats). Message
    read/unread state is irrelevant — full visible history within the window is read.
    """
    start, end = date_window(cfg)
    LOG.info("Report window: %s .. %s", start, end)

    profile = ROOT / cfg["scrape"]["profile_dir"]
    profile.mkdir(exist_ok=True)

    gset = _gentle(cfg)
    raw_dir = p(cfg, "raw")
    # With a per-run cap we RESUME (keep already-scraped groups, do the next batch).
    # Otherwise clear stale raw so a failed scrape never reuses old/sample files.
    if gset["cap"] > 0:
        LOG.info("Gentle cap=%d active: resume mode (keeping already-scraped groups).", gset["cap"])
    else:
        for old in raw_dir.glob("*.json"):
            old.unlink()
        LOG.info("Cleared %s of previous raw data.", raw_dir.name)

    index = []
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=cfg["scrape"]["headless"],
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WA_URL, wait_until="domcontentloaded")
        wait_for_login(page)

        if inspect:
            data = wait_for_chat_list(page)
            diag = page.evaluate(CHATLIST_JS)
            print("\n" + "=" * 60)
            print(" WHATSAPP WEB CHAT-LIST DIAGNOSTIC")
            print("=" * 60)
            print(" pane found      :", diag.get("ok"), diag.get("reason", ""))
            print(" role used       :", diag.get("rowsel"))
            print(" element counts  :", diag.get("counts"))
            print(" scroll (top/h/c):", diag.get("scrollTop"), diag.get("scrollHeight"),
                  diag.get("clientHeight"))
            titles = diag.get("titles", [])
            print(f" visible titles  : {len(titles)}")
            for t in titles[:25]:
                print("    -", t)
            dump = p(cfg, "processed") / "page_dump.html"
            dump.write_text(page.content(), encoding="utf-8")
            print("\n Full HTML dumped to:", dump)
            print("=" * 60)
            ctx.close()
            return []

        wait_for_chat_list(page)
        groups = []
        if cfg.get("keywords"):
            LOG.info("Discovering chats matching keywords...")
            groups = discover_groups(page, cfg["keywords"])
        for t in cfg.get("target_chats", []) or []:   # explicit chats/groups
            if t not in groups:
                groups.append(t)
        LOG.info("Targeting %d chat(s): %s", len(groups), groups)

        # resume + cap: skip groups already scraped this cycle, take next batch
        todo = groups
        if gset["cap"] > 0:
            remaining = [g for g in groups if not (raw_dir / f"{slug(g)}.json").exists()]
            done = len(groups) - len(remaining)
            todo = remaining[:gset["cap"]]
            LOG.info("Resume: %d already done, %d remaining; scraping %d this run.",
                     done, len(remaining), len(todo))
            if not todo:
                LOG.info("All matched groups already scraped this cycle. "
                         "Delete data/raw to start a fresh cycle, or use --skip-scrape to report.")

        for gi, g in enumerate(todo):
            LOG.info("Scraping group %d/%d: %s", gi + 1, len(todo), g)
            if not open_group(page, g):
                LOG.warning("  could not open %s — skipping", g)
                continue
            raw = scrape_group(page, g, start, cfg)
            doc = materialise(g, raw, start, cfg)
            out = p(cfg, "raw") / f"{slug(g)}.json"
            write_json(out, doc)
            LOG.info("  saved %d messages -> %s", doc["message_count"], out.name)
            if gi < len(todo) - 1:
                _group_pause(cfg)

        # build the index from everything currently on disk (covers resume runs)
        for f in sorted(raw_dir.glob("*.json")):
            if f.name.startswith("_"):
                continue
            d = read_json(f) or {}
            index.append({"group": d.get("group", f.stem), "file": f.name,
                          "messages": d.get("message_count", 0)})
        write_json(raw_dir / "_index.json",
                   {"window": [start.isoformat(), end.isoformat()], "groups": index})
        LOG.info("Done. %d chats on disk (%d scraped this run).", len(index), len(todo))
        ctx.close()
    return index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="dump page HTML for debugging and exit")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    run(load_config(args.config), inspect=args.inspect)


if __name__ == "__main__":
    main()
