"""One-shot diagnostic: open ONE chat and report exactly what happens.

Run:
    python probe_click.py "AOL-Houston Volunteers"

It tries to locate + click the chat, then prints which click method worked and
what the opened conversation's DOM looks like (so we can pin the right selectors
instead of guessing). Also dumps the post-click HTML.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_config, p  # noqa: E402
import scrape_whatsapp as S  # noqa: E402

POST_CLICK_JS = r"""
() => {
  const q = s => { try { return document.querySelectorAll(s).length; } catch(e){ return -1; } };
  const main = document.querySelector('#main');
  let header = null;
  if (main) { const h = main.querySelector('header span[title], header span[dir="auto"]');
              header = h ? (h.getAttribute('title') || h.innerText) : null; }
  // find the biggest scrollable area (likely the message list)
  let bigScroll = null, bigH = 0;
  document.querySelectorAll('div').forEach(d => {
    if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > 200 && d.scrollHeight > bigH) {
      bigH = d.scrollHeight; bigScroll = d;
    }
  });
  return {
    has_main: !!main,
    header_title_of_open_chat: header,
    main_div_role_row: main ? main.querySelectorAll('div[role="row"]').length : 0,
    data_pre_plain_text: q('[data-pre-plain-text]'),
    selectable_text: q('span.selectable-text'),
    role_application: q('div[role="application"]'),
    conversation_testid: q('[data-testid="conversation-panel-body"], [data-testid="conversation-panel-messages"]'),
    composer_textbox: q('div[contenteditable="true"][role="textbox"]'),
    biggest_scroll_clientH: bigScroll ? bigScroll.clientHeight : 0,
    biggest_scroll_scrollH: bigScroll ? bigScroll.scrollHeight : 0,
    imgs_in_main: main ? main.querySelectorAll('img').length : 0,
  };
}
"""


def main():
    cfg = load_config()
    target = sys.argv[1] if len(sys.argv) > 1 else None
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / cfg["scrape"]["profile_dir"]),
            headless=False, viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(S.WA_URL, wait_until="domcontentloaded")
        S.wait_for_login(page)
        S.wait_for_chat_list(page)

        if not target:
            groups = S.discover_groups(page, cfg.get("keywords") or [])
            target = groups[0] if groups else None
        print("\n================ PROBE ================")
        print("TARGET:", repr(target))
        if not target:
            print("No target. Pass a chat title as an argument.")
            ctx.close(); return

        pane = page.locator("#pane-side")
        located = False
        clicked_method = "none"
        page.evaluate(S.SCROLL_CHATLIST_JS, 0)
        page.wait_for_timeout(500)
        for i in range(400):
            loc = pane.get_by_title(target, exact=True)
            cnt = loc.count()
            if cnt > 0:
                located = True
                print(f"get_by_title found {cnt} match(es) after {i} scroll step(s).")
                try:
                    loc.first.scroll_into_view_if_needed(timeout=3000)
                    loc.first.click(timeout=5000)
                    clicked_method = "playwright-native"
                    print("native click: OK")
                except Exception as e:
                    print("native click FAILED:", str(e)[:150])
                    ok = page.evaluate(S.JS_REAL_CLICK, target)
                    clicked_method = "js-dispatch" if ok else "js-dispatch-failed"
                    print("JS dispatch click returned:", ok)
                break
            d = page.evaluate(S.CHATLIST_JS)
            if d.get("atBottom"):
                break
            page.evaluate(S.SCROLL_CHATLIST_JS, 0.85)
            page.wait_for_timeout(350)

        if not located:
            print("get_by_title NEVER located the title. Trying JS dispatch on current view...")
            print("JS dispatch returned:", page.evaluate(S.JS_REAL_CLICK, target))

        page.wait_for_timeout(3500)
        diag = page.evaluate(POST_CLICK_JS)
        print("\nclick_method:", clicked_method)
        print("POST-CLICK DIAGNOSTIC:")
        print(json.dumps(diag, indent=2, ensure_ascii=False))

        # ---- TEXT EXTRACTION TEST: scroll up a few times, extract messages ----
        print("\n--- TEXT EXTRACTION TEST (scrolling up 6x) ---")
        collected = {}
        for r in range(6):
            data = page.evaluate(S.EXTRACT_JS)
            for m in data["messages"]:
                collected[m["key"]] = m
            page.evaluate(S.SCROLL_UP_JS)
            page.wait_for_timeout(700)
        withtext = sum(1 for m in collected.values() if (m.get("text") or "").strip())
        withimg = sum(1 for m in collected.values() if m.get("images"))
        print(f"extracted {len(collected)} rows | withtext={withtext} | withimage={withimg}")
        shown = 0
        for m in collected.values():
            t = (m.get("text") or "").strip()
            if t:
                print("   TEXT:", repr(t[:70]), "| pretext:", repr((m.get("pretext") or "")[:40]))
                shown += 1
            if shown >= 6:
                break

        dump = p(cfg, "processed") / "after_click_dump.html"
        dump.write_text(page.content(), encoding="utf-8")
        print("\nHTML after click dumped to:", dump)
        print("======================================\n")
        page.wait_for_timeout(1500)
        ctx.close()


if __name__ == "__main__":
    main()
