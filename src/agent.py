"""Standalone, self-contained WhatsApp report agent.

Runs entirely on your machine and talks ONLY to the local model via Ollama —
no Claude, no cloud, zero external AI usage.

  python src/agent.py
      → launches the interactive MENU (src/menu.py): every option is shown,
        the last 3 requests are offered, and progress is displayed live.

  python src/agent.py --intent "last 10 days, my volunteer and events groups, focus on
        events and conflicts, give me a pdf and a csv of events" --yes
  python src/agent.py --skip-scrape ...     # reuse already-scraped data in data/raw
  python src/agent.py --report-only ...     # rebuild report from processed data (instant)

The --intent/--skip-scrape/--report-only flags are for scripting; the menu exposes
all of them as plain options so nothing is hidden.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

from common import (LOG, ROOT, Ollama, date_window, load_config, p, read_json,
                    wait_for_ollama, write_json)

import scrape_whatsapp
import ocr_images
import analyze
import report as report_mod
import render_pdf
import progress

# Windows consoles default to cp1252; force UTF-8 so box/checkmark glyphs print.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ALL_SECTIONS = ["trend", "sentiment", "categories", "groupvol",
                "participation", "group_digests", "events"]
ALL_OUTPUTS = ["pdf", "html", "csv", "json"]

PLAN_SYS = ("You translate a user's plain-language request into a STRICT JSON run-plan "
            "for a WhatsApp community-analytics report. Output JSON only.")


def parse_intent(cfg: dict, text: str) -> dict:
    o = Ollama(cfg)
    today = dt.date.today().isoformat()
    prompt = f"""Today's date is {today}.
Convert the user's request into a JSON object with EXACTLY these keys:

{{
  "days_back": <integer number of past days to cover; if they say "X weeks" use X*7; default 21>,
  "group_keywords": [<case-insensitive substrings that a chat/group TITLE must contain to be included>],
  "explicit_chats": [<exact chat or group names the user explicitly named, group OR one-to-one>],
  "sections": [<any of the section codes below>],
  "outputs": [<any of: "pdf","html","csv","json">],
  "focus": "<=1 sentence on what the user most wants highlighted, else empty string>",
  "title": "<a fitting report title, else empty string>"
}}

Section codes (use these exact strings):
- "trend"          = daily activity volume chart over time
- "sentiment"      = sentiment breakdown + overall discussion-health gauges
- "categories"     = content category distribution (Events/Knowledge/Volunteering/etc.)
- "groupvol"       = bar chart of message VOLUME per group
- "participation"  = top contributors + engagement stats
- "group_digests"  = per-group summary CARDS with conversation-health (pick this for
                     "group digest", "per-group summary", "group breakdown", "health per group")
- "events"         = extracted events table (upcoming first, past last)

Rules:
- If the user does NOT restrict which analyses they want, include ALL sections.
- If the user does NOT mention an output format, use ["pdf","html"].
- If the user names groups only by theme/keyword (e.g. "the volunteer groups"), put those in group_keywords.
- If they name specific chats/people, put the chat/contact NAME ONLY in explicit_chats
  (e.g. "Priya", NOT "my chat with Priya"; "Family", NOT "the Family group").
- If the user gives no group hints at all, leave both lists empty.

User request:
\"\"\"{text}\"\"\"
"""
    plan = o.chat_json(prompt, system=PLAN_SYS)
    norm = _normalize(plan)
    low = text.lower()
    # anti-hallucination: keep only group keywords / chat names that actually
    # appear in the user's request (the small model sometimes invents them).
    norm["group_keywords"] = [k for k in norm["group_keywords"] if k.lower() in low]
    # completeness: users often LIST their group keywords in quotes — capture those
    # directly so the small model can't silently drop one.
    if any(w in low for w in ("group", "chat", "having words", "titles", "named")):
        reserved = set(ALL_SECTIONS) | set(ALL_OUTPUTS)
        existing = {k.lower() for k in norm["group_keywords"]}
        for q in re.findall(r'"([^"]{2,40})"|“([^”]{2,40})”|\'([^\']{2,40})\'', text):
            tok = next((s for s in q if s), "").strip()
            if tok and tok.lower() not in reserved and tok.lower() not in existing:
                norm["group_keywords"].append(tok)
                existing.add(tok.lower())
    norm["explicit_chats"] = [c for c in norm["explicit_chats"]
                              if any(w in low for w in c.lower().split() if len(w) > 2)]
    # Sections default to ALL unless the user EXPLICITLY restricts. Small models
    # tend to drop sections (e.g. events) even when the user wanted a full report,
    # so we only honour a narrowed list when restriction language is present.
    restrict = any(w in low for w in (" only", "just ", "nothing but", "exclude",
                                      "without", "drop ", "skip ", "leave out",
                                      "no events", "no trend", "no sentiment"))
    if not restrict:
        norm["sections"] = list(ALL_SECTIONS)
    return norm


def _normalize(plan: dict) -> dict:
    def lst(x):
        return [s for s in x if isinstance(s, str) and s.strip()] if isinstance(x, list) else []
    days = plan.get("days_back", 21)
    try:
        days = max(1, min(365, int(days)))
    except Exception:
        days = 21
    sections = [s for s in lst(plan.get("sections")) if s in ALL_SECTIONS] or ALL_SECTIONS
    outputs = [o for o in lst(plan.get("outputs")) if o in ALL_OUTPUTS] or ["pdf", "html"]
    if "pdf" in outputs and "html" not in outputs:
        outputs.append("html")  # pdf is rendered from html

    def clean_text(v: str) -> str:
        v = (v or "").strip()
        low = v.lower()
        # drop schema placeholders the model sometimes echoes verbatim
        if not v or "<" in v or "sentence on what" in low or "fitting report title" in low \
           or "else empty string" in low:
            return ""
        return v

    return {
        "days_back": days,
        "group_keywords": lst(plan.get("group_keywords")),
        "explicit_chats": lst(plan.get("explicit_chats")),
        "sections": sections,
        "outputs": outputs,
        "focus": clean_text(plan.get("focus")),
        "title": clean_text(plan.get("title")),
    }


def build_cfg(base: dict, plan: dict) -> dict:
    cfg = copy.deepcopy(base)
    cfg["date_range"]["days_back"] = plan["days_back"]
    # group selection comes ENTIRELY from the user's request — no config defaults
    cfg["keywords"] = plan["group_keywords"]
    cfg["target_chats"] = plan["explicit_chats"]
    cfg.setdefault("report", {})
    cfg["report"]["include"] = plan["sections"]
    cfg["report"]["focus"] = plan["focus"]
    if plan["title"]:
        cfg["report"]["title"] = plan["title"]
    return cfg


def show_plan(cfg: dict, plan: dict) -> None:
    start, end = date_window(cfg)
    print("\n" + "═" * 64)
    print(" RUN PLAN  (interpreted locally by Qwen — no cloud)")
    print("═" * 64)
    print(f"  Period          : last {plan['days_back']} days  ({start} → {end})")
    print(f"  Group keywords  : {', '.join(cfg['keywords']) or '(none)'}")
    print(f"  Explicit chats  : {', '.join(plan['explicit_chats']) or '(none)'}")
    print(f"  Report sections : {', '.join(plan['sections'])}")
    print(f"  Outputs         : {', '.join(plan['outputs'])}")
    print(f"  Focus           : {plan['focus'] or '(general)'}")
    print(f"  Title           : {cfg['report']['title']}")
    print("═" * 64 + "\n")


EVENT_COLS = ["status", "title", "date", "time", "venue", "location",
              "conference_link", "registration_link", "contact", "host", "group"]


def export_events(cfg: dict) -> None:
    """Always export the events as their own JSON + CSV (a fixed deliverable)."""
    a = read_json(p(cfg, "processed") / "analysis.json") or {}
    events = [{k: e.get(k, "") for k in EVENT_COLS} for e in a.get("events", [])]
    write_json(p(cfg, "output") / "events.json",
               {"window": a.get("window", {}), "count": len(events), "events": events})
    with open(p(cfg, "output") / "events.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EVENT_COLS)
        for e in events:
            w.writerow([e[k] for k in EVENT_COLS])
    LOG.info("Events export -> events.json , events.csv (%d events)", len(events))


def export_json(cfg: dict) -> None:
    src = p(cfg, "processed") / "analysis.json"
    if src.exists():
        shutil.copy(src, p(cfg, "output") / "report_data.json")


def data_signature(cfg: dict, plan: dict) -> str:
    """Identifies the underlying data+analysis. Excludes report-only choices
    (sections/outputs/title) so pure formatting tweaks don't re-run analysis."""
    return json.dumps({
        "days_back": plan["days_back"],
        "keywords": sorted(cfg.get("keywords", [])),
        "chats": sorted(plan["explicit_chats"]),
        "focus": plan["focus"],
    }, sort_keys=True)


def execute(cfg: dict, plan: dict, skip_scrape: bool, force_scrape: bool = False,
            report_only: bool = False) -> None:
    # start from a clean output folder so the file list reflects only this run
    outdir = p(cfg, "output")
    for f in outdir.glob("*"):
        if f.suffix in {".pdf", ".html", ".csv", ".json"}:
            f.unlink()

    analysis_path = p(cfg, "processed") / "analysis.json"

    # report-only: rebuild the report straight from the already-processed data.
    # No scraping, no OCR, no Qwen analysis — instant, for layout/section/output tweaks.
    if report_only:
        if not analysis_path.exists():
            print("\nNothing to render yet — there's no processed data "
                  "(data/processed/analysis.json). Run a full report first.")
            return
        progress.stage(1, 1, "Rebuilding report from existing analysis (no scrape/analyse)")
        _render_outputs(cfg, plan, outdir)
        return

    sig = data_signature(cfg, plan)
    last_sig = read_json(p(cfg, "processed") / "last_run.json", default={}) or {}
    analysis_exists = analysis_path.exists()
    data_unchanged = (sig == last_sig.get("sig")) and analysis_exists and not force_scrape

    if data_unchanged:
        progress.stage(1, 1, "Data unchanged — reusing existing analysis (formatting-only update)")
    else:
        # total pipeline steps for the progress header
        nsteps = (4 if not skip_scrape else 3) + 1  # +1 = Build report
        step = 0
        if not skip_scrape:
            step += 1
            progress.stage(step, nsteps, "Scraping WhatsApp Web")
            index = scrape_whatsapp.run(cfg)
            if not index:
                print("\n" + "!" * 64)
                print(" NO CHATS WERE SCRAPED — stopping (no stale data will be reused).")
                print(" Likely cause: no group title matched your keywords, or the")
                print(" WhatsApp Web page layout differs from what the scraper expects.")
                print(" Diagnose by dumping the live page, then share it for a quick fix:")
                print("     python src/scrape_whatsapp.py --inspect")
                print(" Or reuse already-scraped data with the 'skip scrape' menu option.")
                print("!" * 64)
                return
        step += 1
        progress.stage(step, nsteps, "Reading images (OCR + local vision)")
        ocr_images.run(cfg)
        step += 1
        progress.stage(step, nsteps, "Analysing (categorise · sentiment · health · events)")
        if analyze.run(cfg) is None:
            print("\nNo messages found in data/raw for this window. Nothing produced.")
            return
        progress.stage(step + 1, nsteps, "Building report")
    write_json(p(cfg, "processed") / "last_run.json",
               {"sig": sig, "keywords": cfg.get("keywords", []),
                "explicit_chats": plan.get("explicit_chats", []),
                "days_back": plan.get("days_back")})

    _render_outputs(cfg, plan, outdir)


def _render_outputs(cfg: dict, plan: dict, outdir) -> None:
    """Fixed deliverables from analysis.json: report.html + report.pdf + events.json + events.csv."""
    LOG.info("Building report.html + report.pdf + events.json + events.csv")
    report_mod.run(cfg)
    render_pdf.run(cfg)
    export_events(cfg)
    print("\n✔ Report ready in:", outdir)
    for f in sorted(outdir.glob("*")):
        if f.suffix in {".pdf", ".html", ".csv", ".json"}:
            print("   -", f.name)


REFRESH_WORDS = ("re-scrape", "rescrape", "refresh", "re-fetch", "refetch",
                 "fetch again", "fetch new", "new data", "update data", "pull again",
                 "scrape again", "latest messages")

# Phrases that mean "just rebuild the report from the data already processed".
REGEN_WORDS = ("report only", "report-only", "regenerate the report", "regenerate report",
               "re-generate report", "rebuild the report", "rebuild report",
               "re-render", "rerender", "render the report", "just the report",
               "from existing data", "from extracted data", "without re-extract",
               "without re-scrap", "don't re-extract", "do not re-extract",
               "reuse the data", "same data")


def one_run(base: dict, text: str, assume_yes: bool, skip_scrape: bool,
            report_only: bool = False) -> None:
    try:
        plan = parse_intent(base, text)
    except Exception as e:
        # report-only must work even with the model offline — fall back to defaults
        LOG.warning("Intent parsing unavailable (%s); using default plan.", str(e)[:60])
        plan = _normalize({})
    cfg = build_cfg(base, plan)
    force_scrape = any(w in text.lower() for w in REFRESH_WORDS)
    report_only = report_only or any(w in text.lower() for w in REGEN_WORDS)
    # No groups named? Treat it as a REFINEMENT of the last report (reuse its
    # groups + already-scraped data, just re-render with the new options) — rather
    # than asking for groups again. Only ask if there's no previous report at all.
    if not cfg["keywords"] and not plan["explicit_chats"] and not force_scrape:
        last = read_json(p(cfg, "processed") / "last_run.json", default={}) or {}
        prev_kw, prev_chats = last.get("keywords") or [], last.get("explicit_chats") or []
        if prev_kw or prev_chats:
            cfg["keywords"] = prev_kw
            plan["explicit_chats"] = prev_chats
            cfg["target_chats"] = prev_chats
            if last.get("days_back"):
                cfg["date_range"]["days_back"] = last["days_back"]
                plan["days_back"] = last["days_back"]
            skip_scrape = True   # refinement never re-scrapes
            LOG.info("No groups named → refining the last report (reusing its groups "
                     "& data, no re-scrape).")
        elif not skip_scrape and not report_only:
            print("\n  Which groups or chats should I cover? Name them by keyword "
                  "(e.g. 'the volunteer groups') or exact name ('chat with Priya').")
            if assume_yes:
                print("  No groups specified — nothing to scrape. Skipping.")
                return
            extra = input("  Groups/chats > ").strip()
            if not extra:
                print("  No groups given. Cancelled.")
                return
            return one_run(base, text + " GROUPS: " + extra, assume_yes, skip_scrape)
    if report_only:
        print("  (report-only: rebuilding from already-processed data — no scrape/analyse)")
    show_plan(cfg, plan)
    if not assume_yes:
        ans = input("Proceed with this plan? [Y/n/edit] ").strip().lower()
        if ans in ("n", "no", "q", "quit"):
            print("Cancelled.")
            return
        if ans in ("e", "edit"):
            print("Describe the change (e.g. 'make it last 7 days, drop events'):")
            tweak = input("> ").strip()
            return one_run(base, text + " ALSO: " + tweak, assume_yes, skip_scrape, report_only)
    execute(cfg, plan, skip_scrape, force_scrape=force_scrape, report_only=report_only)


def main():
    ap = argparse.ArgumentParser(description="Local WhatsApp report agent (zero cloud usage).")
    ap.add_argument("--intent", help="natural-language description of the report you want")
    ap.add_argument("--yes", action="store_true", help="run without confirmation")
    ap.add_argument("--skip-scrape", action="store_true", help="reuse existing data in data/raw")
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild the report from already-processed data (no scrape/analyse)")
    args = ap.parse_args()

    base = load_config()

    # Scripted (non-interactive) path: a one-shot run from CLI flags.
    if args.intent:
        if not args.report_only and not wait_for_ollama(base):
            print("Ollama is not running. Start it (`ollama serve`) and retry.")
            sys.exit(1)
        one_run(base, args.intent, args.yes, args.skip_scrape, report_only=args.report_only)
        return

    # Interactive path: the full menu-driven console (all options discoverable).
    import menu
    menu.run(base)


if __name__ == "__main__":
    main()
