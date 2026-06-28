"""Menu-driven console for the Offline AI WhatsApp Message Aggregator.

Two inputs only — (1) how far back, (2) which groups/people. The extraction
criteria are shown up front (and editable), and the output format is fixed:
report.html + report.pdf + events.json + events.csv.
"""
from __future__ import annotations

import datetime as dt
import re
import sys

from common import ROOT, Ollama, date_window, load_config, p, read_json, write_json, wait_for_ollama
import agent

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LINE = "=" * 68


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "0"


def header(t: str) -> None:
    print("\n" + LINE + "\n " + t + "\n" + LINE)


# ── history of (window, groups) inputs ──────────────────────────────────────
def _hist_path(base):
    return p(base, "processed") / "input_history.json"


def load_history(base):
    return read_json(_hist_path(base), default=[]) or []


def save_history(base, entry):
    h = [entry] + [x for x in load_history(base)
                   if not (x.get("label") == entry["label"] and x.get("groups") == entry["groups"])]
    write_json(_hist_path(base), h[:10])


# ── criteria display ─────────────────────────────────────────────────────────
def show_criteria(base):
    ex = base.get("extraction", {}) or {}
    print("\n  WHAT THIS EXTRACTS (default — edit in option 3 or config.yaml):")
    for c in ex.get("capture", []):
        print("    •", c)
    print(f"    • Greetings/thanks/birthdays → grouped at the END "
          f"({'on' if ex.get('pleasantries_last', True) else 'off'})")
    print(f"    • WhatsApp system notices dropped: {ex.get('drop_system_messages', True)}")
    print(f"    • Skip chats with < {ex.get('min_messages_per_group', 3)} messages")


# ── window + groups input ───────────────────────────────────────────────────
def ask_window():
    print("\n  How far back?")
    print("    • Enter a number of PAST DAYS  (e.g. 21)")
    print("    • Or type 'r' for a specific date range")
    v = ask("  Days back (or r): ").lower()
    if v == "r":
        s = ask("  Start date (YYYY-MM-DD): ")
        e = ask("  End date   (YYYY-MM-DD): ")
        try:
            sd, ed = dt.date.fromisoformat(s), dt.date.fromisoformat(e)
            if ed < sd:
                sd, ed = ed, sd
            return {"days_back": max(1, (ed - sd).days), "end": ed.isoformat(),
                    "label": f"{sd} → {ed}"}
        except Exception:
            print("  ! Invalid dates."); return None
    if v.isdigit() and int(v) > 0:
        return {"days_back": int(v), "end": None, "label": f"last {v} days"}
    print("  ! Enter a number of days or 'r'."); return None


def ask_groups(base):
    print("\n  Which groups / people? (comma-separated patterns that match chat titles,")
    print("   e.g.  Volunteers, Wellness, Events, Run Club, Priya)")
    hist = load_history(base)[:3]
    if hist:
        print("  Recent:")
        for i, h in enumerate(hist, 1):
            print(f"    {i}) {h['label']}  —  {', '.join(h['groups'])[:60]}")
        print("    (or just type new groups below)")
    g = ask("  Groups/people: ")
    if g.isdigit() and 1 <= int(g) <= len(hist):
        h = hist[int(g) - 1]
        return h["groups"], h  # reuse window too
    groups = [x.strip() for x in g.split(",") if x.strip()]
    return groups, None


def flow_generate(base, mode: str):
    if mode != "report" and not wait_for_ollama(base, timeout=3):
        print("\n  ⚠ Ollama isn't running — start it (`ollama serve`). (Report-only works without it.)")
        return
    show_criteria(base)
    groups, reuse = ask_groups(base)
    if reuse:
        win = {"days_back": reuse["days_back"], "end": reuse.get("end"), "label": reuse["label"]}
    else:
        if not groups and mode == "full":
            print("  ! No groups given."); return
        win = ask_window()
        if not win:
            return
    save_history(base, {"label": win["label"], "groups": groups,
                        "days_back": win["days_back"], "end": win.get("end")})

    plan = {"days_back": win["days_back"], "group_keywords": groups, "explicit_chats": [],
            "sections": agent.ALL_SECTIONS, "outputs": ["html", "pdf"], "focus": "", "title": ""}
    cfg = agent.build_cfg(base, plan)
    if win.get("end"):
        cfg["date_range"]["end"] = win["end"]
    start, end = date_window(cfg)

    header("CONFIRM")
    print(f"   Window  : {win['label']}   ({start} → {end})")
    print(f"   Groups  : {', '.join(groups) or '(none — full run needs groups)'}")
    print(f"   Mode    : {'Full scrape → analyse → report' if mode == 'full' else ('Re-analyse downloaded data' if mode == 'skip' else 'Rebuild report only')}")
    print("   Output  : report.html + report.pdf + events.json + events.csv")
    if ask("\n  Run now? [Y/n] ").lower() in ("n", "no", "0"):
        print("  Cancelled."); return
    agent.execute(cfg, plan, skip_scrape=(mode in ("skip", "report")), report_only=(mode == "report"))
    ask("\n  Press Enter to return to the menu… ")


# ── criteria editor ─────────────────────────────────────────────────────────
def _set_cfg(pattern, replacement):
    f = ROOT / "config.yaml"
    text = f.read_text(encoding="utf-8")
    new, n = re.subn(pattern, replacement, text, count=1)
    if n:
        f.write_text(new, encoding="utf-8")
    return bool(n)


def edit_criteria(base):
    while True:
        cfg = load_config()
        ex = cfg.get("extraction", {})
        header("EXTRACTION CRITERIA")
        for c in ex.get("capture", []):
            print("   •", c)
        print(f"\n   1) Pleasantries grouped last : {ex.get('pleasantries_last', True)}")
        print(f"   2) Drop WhatsApp system notices: {ex.get('drop_system_messages', True)}")
        print(f"   3) Min messages per group     : {ex.get('min_messages_per_group', 3)}")
        print("   4) Edit keyword lists          → open config.yaml (extraction: section)")
        print("   0) Back")
        s = ask("\n Select: ")
        if s in ("0", ""):
            return
        elif s == "1":
            _set_cfg(r'(\n\s*pleasantries_last:\s*)(true|false)',
                     rf'\g<1>{str(not ex.get("pleasantries_last", True)).lower()}')
        elif s == "2":
            _set_cfg(r'(\n\s*drop_system_messages:\s*)(true|false)',
                     rf'\g<1>{str(not ex.get("drop_system_messages", True)).lower()}')
        elif s == "3":
            v = ask("  Minimum messages per group: ")
            if v.isdigit():
                _set_cfg(r'(\n\s*min_messages_per_group:\s*)\d+', rf'\g<1>{int(v)}')
        elif s == "4":
            print(f"  Edit the 'extraction:' section in {ROOT / 'config.yaml'} "
                  "(pleasantry_keywords, system_message_patterns, event_link_domains).")
            ask("  Press Enter… ")


# ── settings (model / gentle) ────────────────────────────────────────────────
def settings(base):
    while True:
        cfg = load_config()
        header("SETTINGS")
        print(f"   Analysis model : {cfg['models']['analysis']}")
        print(f"   Gentle scraping: {cfg['scrape'].get('gentle_mode', True)}")
        print("\n   1) Change analysis model")
        print("   2) Toggle gentle scraping")
        print("   0) Back")
        s = ask("\n Select: ")
        if s in ("0", ""):
            return
        elif s == "1":
            models = Ollama(cfg).available_models()
            if not models:
                print("  Could not list models (is Ollama running?)."); continue
            for i, m in enumerate(models, 1):
                print(f"   {i}) {m}")
            ch = ask("  Pick analysis model #: ")
            if ch.isdigit() and 1 <= int(ch) <= len(models):
                _set_cfg(r'(\n\s*analysis:\s*)"[^"]*"', rf'\g<1>"{models[int(ch)-1]}"')
                print("  ✓ saved.")
        elif s == "2":
            cur = bool(cfg["scrape"].get("gentle_mode", True))
            _set_cfg(r'(\n\s*gentle_mode:\s*)(true|false)', rf'\g<1>{str(not cur).lower()}')
            print(f"  ✓ gentle_mode = {str(not cur).lower()}")


def open_output():
    out = p(load_config(), "output")
    print(f"\n  Files in {out}:")
    for f in sorted(out.glob("*")):
        if f.suffix in {".pdf", ".html", ".csv", ".json"}:
            print("   -", f.name)
    ask("  Press Enter… ")


def about():
    header("HELP / ABOUT")
    print(" Offline AI WhatsApp Message Aggregator — fully on-device (Ollama + Tesseract).")
    print(" You give two things: how far back, and which groups/people. It produces a")
    print(" fixed report (HTML + PDF) plus events.json + events.csv.\n")
    print(" It compacts & de-duplicates across groups, captures every unique discussion")
    print(" (with who took part) and every event (date/time/venue/location/links/host),")
    print(" and lists greetings/birthdays/thanks separately at the end.\n")
    print(" 1 Full run        — scrape WhatsApp, analyse, build report.")
    print(" 2 Re-analyse      — reuse already-downloaded data (no scrape).")
    print(" 3 Rebuild report  — re-render from the last analysis (instant).")
    print(" 4 Criteria        — see/adjust what is extracted.")
    print(" 5 Settings        — AI model, gentle scraping.\n")
    print(" ⚠ Unofficial & against WhatsApp ToS; read-only; personal use only.")
    ask(" Press Enter… ")


def run(base=None):
    base = base or load_config()
    while True:
        header("OFFLINE AI WHATSAPP MESSAGE AGGREGATOR")
        print("   1) Generate report      (Full: scrape → analyse → report)")
        print("   2) Generate report      (Re-analyse already-downloaded data)")
        print("   3) Rebuild report only  (instant, from last analysis)")
        print("   4) Extraction criteria  (see / adjust what is captured)")
        print("   5) Settings             (AI model, gentle scraping)")
        print("   6) Open output folder")
        print("   7) Help / About")
        print("   0) Quit")
        s = ask("\n Select: ")
        if s in ("0", "q", "quit", "exit"):
            print(" Bye."); return
        elif s == "1":
            flow_generate(base, "full")
        elif s == "2":
            flow_generate(base, "skip")
        elif s == "3":
            flow_generate(base, "report")
        elif s == "4":
            edit_criteria(base)
        elif s == "5":
            settings(base)
        elif s == "6":
            open_output()
        elif s == "7":
            about()
        else:
            print("  Please choose a number from the menu.")


if __name__ == "__main__":
    run()
