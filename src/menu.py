"""Menu-driven console front-end for the WhatsApp report agent.

Every option is shown explicitly (no hidden flags): run modes, query history,
and a full plan-review screen where each parameter can be changed before running.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from common import ROOT, Ollama, date_window, load_config, p, read_json, write_json, wait_for_ollama
import agent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LINE = "=" * 66
HIST_MAX = 10


# ── small input helpers ─────────────────────────────────────────────────────
def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "0"


def header(title: str) -> None:
    print("\n" + LINE)
    print(" " + title)
    print(LINE)


# ── query history ───────────────────────────────────────────────────────────
def _hist_path(base):
    return p(base, "processed") / "query_history.json"


def load_history(base) -> list[str]:
    return read_json(_hist_path(base), default=[]) or []


def save_query(base, q: str) -> None:
    q = q.strip()
    if not q:
        return
    h = [q] + [x for x in load_history(base) if x != q]
    write_json(_hist_path(base), h[:HIST_MAX])


def choose_query(base) -> str | None:
    """Show the last 3 queries; let the user pick one or write a new one."""
    hist = load_history(base)[:3]
    header("CHOOSE A REQUEST")
    if hist:
        print(" Recent requests:")
        for i, q in enumerate(hist, 1):
            print(f"   {i}) {q[:90]}")
    else:
        print(" (no previous requests yet)")
    print("   n) Write a NEW request")
    print("   0) Back")
    sel = ask("\n Select: ").lower()
    if sel in ("0", ""):
        return None
    if sel == "n":
        q = ask(" Describe the report you want:\n > ")
        return q or None
    if sel.isdigit() and 1 <= int(sel) <= len(hist):
        return hist[int(sel) - 1]
    # anything else typed → treat it as a brand-new request
    return sel or None


# ── set editor (sections / outputs) ─────────────────────────────────────────
def edit_set(title: str, all_items: list[str], current: list[str]) -> list[str]:
    cur = set(current)
    while True:
        print("\n " + title)
        for i, it in enumerate(all_items, 1):
            print(f"   {i}) [{'x' if it in cur else ' '}] {it}")
        print("   a) select all    c) clear all    d) done")
        sel = ask(" toggle number, or a/c/d: ").lower()
        if sel == "d":
            break
        if sel == "a":
            cur = set(all_items); continue
        if sel == "c":
            cur = set(); continue
        if sel.isdigit() and 1 <= int(sel) <= len(all_items):
            it = all_items[int(sel) - 1]
            cur.discard(it) if it in cur else cur.add(it)
    return [it for it in all_items if it in cur] or list(all_items)


# ── plan review / edit ──────────────────────────────────────────────────────
MODE_LABEL = {
    "full":   "Full run  (scrape WhatsApp → analyse → report)",
    "skip":   "Skip scraping  (re-analyse data already downloaded)",
    "report": "Report only  (instant rebuild from processed data)",
}


def plan_review(base, plan: dict, mode: str) -> None:
    while True:
        cfg = agent.build_cfg(base, plan)
        start, end = date_window(cfg)
        header("REVIEW & ADJUST  —  change anything before running")
        print(f"   Mode            : {MODE_LABEL[mode]}")
        print(f"   Time window     : last {plan['days_back']} days   ({start} → {end})")
        print(f"   Group keywords  : {', '.join(plan['group_keywords']) or '(none)'}")
        print(f"   Explicit chats  : {', '.join(plan['explicit_chats']) or '(none)'}")
        print(f"   Report sections : {', '.join(plan['sections'])}")
        print(f"   Outputs         : {', '.join(plan['outputs'])}")
        print(f"   Focus           : {plan['focus'] or '(general)'}")
        print(f"   Title           : {cfg['report']['title']}")
        print("\n   1) RUN with these settings")
        print("   2) Change time window (days)")
        print("   3) Change groups / chats")
        print("   4) Change report sections")
        print("   5) Change outputs")
        print("   6) Change run mode")
        print("   7) Change focus / title")
        print("   0) Cancel")
        sel = ask("\n Select: ").lower()

        if sel == "0":
            print(" Cancelled."); return
        elif sel == "1":
            if mode == "full" and not plan["group_keywords"] and not plan["explicit_chats"]:
                print("\n  ⚠ A full run needs at least one group/chat. Use option 3 first.")
                continue
            _run(base, plan, mode)
            return
        elif sel == "2":
            v = ask(" Number of past days to cover: ")
            if v.isdigit():
                plan["days_back"] = max(1, min(365, int(v)))
        elif sel == "3":
            kw = ask(" Group keywords (comma-separated, matches chat titles): ")
            ch = ask(" Exact chat/contact names (comma-separated, optional): ")
            if kw or ch:
                plan["group_keywords"] = [s.strip() for s in kw.split(",") if s.strip()]
                plan["explicit_chats"] = [s.strip() for s in ch.split(",") if s.strip()]
        elif sel == "4":
            plan["sections"] = edit_set("REPORT SECTIONS (toggle):", agent.ALL_SECTIONS, plan["sections"])
        elif sel == "5":
            plan["outputs"] = edit_set("OUTPUT FORMATS (toggle):", agent.ALL_OUTPUTS, plan["outputs"])
            if "pdf" in plan["outputs"] and "html" not in plan["outputs"]:
                plan["outputs"].append("html")
        elif sel == "6":
            mode = _choose_mode(mode)
        elif sel == "7":
            f = ask(" Focus (what to highlight; blank = general): ")
            t = ask(" Report title (blank = keep current): ")
            plan["focus"] = f
            if t:
                plan["title"] = t


def _choose_mode(current: str) -> str:
    header("RUN MODE")
    keys = ["full", "skip", "report"]
    for i, k in enumerate(keys, 1):
        print(f"   {i}) {MODE_LABEL[k]}{'   ← current' if k == current else ''}")
    sel = ask("\n Select: ")
    return keys[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= 3 else current


def _run(base, plan: dict, mode: str) -> None:
    skip_scrape = mode in ("skip", "report")
    report_only = mode == "report"
    cfg = agent.build_cfg(base, plan)
    agent.execute(cfg, plan, skip_scrape=skip_scrape, report_only=report_only)
    ask("\n Press Enter to return to the menu… ")


# ── generate-report flow ────────────────────────────────────────────────────
def flow_generate(base, mode: str) -> None:
    if mode != "report" and not wait_for_ollama(base, timeout=3):
        print("\n  ⚠ Ollama isn't running — start it (`ollama serve`) for scraping/analysis.")
        print("    (Report-only mode works without it.)")
        return
    q = choose_query(base)
    if not q:
        return
    save_query(base, q)
    print("\n  Interpreting your request with the local model… (no cloud)")
    try:
        plan = agent.parse_intent(base, q)
    except Exception as e:
        print(f"  (model unavailable: {str(e)[:60]} — using defaults)")
        plan = agent._normalize({})
    # report-only with no groups: reuse last run's groups so the plan looks right
    if not plan["group_keywords"] and not plan["explicit_chats"]:
        last = read_json(p(base, "processed") / "last_run.json", default={}) or {}
        plan["group_keywords"] = last.get("keywords") or plan["group_keywords"]
        plan["explicit_chats"] = last.get("explicit_chats") or plan["explicit_chats"]
    plan_review(base, plan, mode)


# ── settings ────────────────────────────────────────────────────────────────
def _set_config_value(pattern: str, replacement: str) -> bool:
    f = ROOT / "config.yaml"
    text = f.read_text(encoding="utf-8")
    new, n = re.subn(pattern, replacement, text, count=1)
    if n:
        f.write_text(new, encoding="utf-8")
    return bool(n)


def settings(base) -> None:
    while True:
        cfg = load_config()  # re-read so edits show immediately
        header("SETTINGS")
        print(f"   Analysis model : {cfg['models']['analysis']}")
        print(f"   Vision model   : {cfg['models']['vision']}")
        print(f"   Gentle mode    : {cfg['scrape'].get('gentle_mode', True)}  (paces scraping to reduce ban risk)")
        print("\n   1) Change analysis model")
        print("   2) Toggle gentle mode")
        print("   3) View key config")
        print("   0) Back")
        sel = ask("\n Select: ")
        if sel in ("0", ""):
            return
        elif sel == "1":
            models = Ollama(cfg).available_models()
            if not models:
                print("  Could not list models (is Ollama running?)."); continue
            print("\n  Installed models:")
            for i, m in enumerate(models, 1):
                print(f"   {i}) {m}")
            ch = ask("  Pick number for ANALYSIS model: ")
            if ch.isdigit() and 1 <= int(ch) <= len(models):
                m = models[int(ch) - 1]
                ok = _set_config_value(r'(\n\s*analysis:\s*)"[^"]*"', rf'\g<1>"{m}"')
                print(f"  {'✓ Saved analysis model = ' + m if ok else '✗ could not update config.yaml'}")
        elif sel == "2":
            cur = bool(cfg["scrape"].get("gentle_mode", True))
            ok = _set_config_value(r'(\n\s*gentle_mode:\s*)(true|false|True|False)',
                                   rf'\g<1>{str(not cur).lower()}')
            print(f"  {'✓ gentle_mode = ' + str(not cur).lower() if ok else '✗ could not update config.yaml'}")
        elif sel == "3":
            print(f"\n  models     : {cfg['models']}")
            print(f"  date window: weeks_back={cfg['date_range'].get('weeks_back')} "
                  f"days_back={cfg['date_range'].get('days_back')}")
            print(f"  tesseract  : {cfg['ocr']['tesseract_cmd']}")
            print(f"  output dir : {cfg['paths']['output']}")
            ask("  Press Enter… ")


def open_output(base) -> None:
    out = p(load_config(), "output")
    print(f"\n  Reports are in: {out}")
    files = sorted(f.name for f in out.glob("*") if f.suffix in {".pdf", ".html", ".csv", ".json"})
    for f in files:
        print("   -", f)
    if not files:
        print("   (none yet — generate a report first)")
    ask("  Press Enter… ")


def about() -> None:
    header("HELP / ABOUT")
    print(" Offline AI WhatsApp Message Aggregator — runs fully on your machine")
    print(" via local models (Ollama) + Tesseract OCR. No cloud, no Claude.\n")
    print(" Menu options:")
    print("   1 Generate report (Full) — scrape WhatsApp, analyse, build report.")
    print("   2 Skip scraping          — re-analyse data already downloaded.")
    print("   3 Report only            — instantly rebuild the report from the")
    print("                              last analysis (change look/sections/outputs).")
    print("   4 Settings               — choose the AI model, toggle gentle mode.")
    print("   5 Open report folder     — list generated files.\n")
    print(" Power users can still script it:")
    print('   python agent.py --intent "last 10 days, my groups, pdf" --yes')
    print("   python agent.py --skip-scrape   |   --report-only\n")
    print(" ⚠ Unofficial & against WhatsApp ToS; read-only; personal use only.")
    ask(" Press Enter… ")


def run(base: dict | None = None) -> None:
    base = base or load_config()
    while True:
        header("OFFLINE AI WHATSAPP MESSAGE AGGREGATOR")
        print("   1) Generate report   (Full: scrape → analyse → report)")
        print("   2) Generate report   (Skip scraping — reuse downloaded data)")
        print("   3) Generate report   (Report only — instant rebuild, no AI)")
        print("   4) Settings          (AI model, gentle mode)")
        print("   5) Open report folder")
        print("   6) Help / About")
        print("   0) Quit")
        sel = ask("\n Select an option: ")
        if sel in ("0", "q", "quit", "exit"):
            print(" Bye."); return
        elif sel == "1":
            flow_generate(base, "full")
        elif sel == "2":
            flow_generate(base, "skip")
        elif sel == "3":
            flow_generate(base, "report")
        elif sel == "4":
            settings(base)
        elif sel == "5":
            open_output(base)
        elif sel == "6":
            about()
        else:
            print("  Please choose a number from the menu.")


if __name__ == "__main__":
    run()
