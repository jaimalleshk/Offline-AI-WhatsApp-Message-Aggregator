"""Generate a SAMPLE report with entirely fake, generic data.

Renders the real template so the repo can ship a representative example without
exposing anyone's real messages, names, photos, or groups. Does NOT touch
data/ or output/, and needs no Ollama.

    python tools/make_sample_report.py   ->  samples/sample_report.html + .pdf
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import report as report_mod          # noqa: E402
import render_pdf                    # noqa: E402
from common import load_config       # noqa: E402

SAMPLES = ROOT / "samples"
SAMPLES.mkdir(exist_ok=True)

START, END = dt.date(2025, 6, 1), dt.date(2025, 6, 21)

trend = []
counts = [4, 9, 6, 2, 1, 12, 15, 7, 5, 8, 3, 2, 10, 14, 6, 4, 9, 7, 3, 11, 16]
d = START
for c in counts:
    trend.append({"date": d.isoformat(), "count": c})
    d += dt.timedelta(days=1)

# ── a fully fake analysis payload (matches the current analyze.py schema) ─────
FAKE = {
    "window": {"start": START.isoformat(), "end": END.isoformat()},
    "generated_at": dt.datetime.now().isoformat(),
    "criteria": {"capture": [
        "Unique discussions (topic + factual summary + who took part, by name)",
        "Events with date, time, venue, location, conference link, registration link, host/contact",
        "Announcements / decisions / important information",
        "Links & resources shared",
    ], "pleasantries_last": True, "min_messages_per_group": 3},
    "groups_covered": ["Riverside Community Volunteers", "Downtown Wellness Group",
                       "City Run Club Organizers"],
    "dedup": {"total_considered": 142, "unique_messages": 116,
              "duplicate_copies_removed": 26, "cross_group_duplicate_topics": 5},
    "stats": {
        "total_unique": 116, "active_participants": 18, "groups_count": 3,
        "discussions_count": 6, "events_count": 4, "pleasantries_count": 23,
        "links_shared": 11, "images_shared": 9, "avg_per_day": 5.5,
        "per_group": [["Riverside Community Volunteers", 52], ["Downtown Wellness Group", 39],
                      ["City Run Club Organizers", 25]],
        "top_contributors": [["Asha R.", 18], ["Ben T.", 15], ["Carlos M.", 13], ["Dana K.", 11]],
        "trend": trend, "busiest_day": {"date": "2025-06-21", "count": 16},
    },
    "highlights": (
        "The Wellness Workshop on 28 June drew strong sign-ups, with five volunteers confirmed for "
        "setup and the registration link shared across two groups. The City Run Club finalised its new "
        "route plan after a recurring debate, and a beginners training session was scheduled for 30 June. "
        "A proposal to trial a small class fee was discussed civilly and agreed for a one-month trial. "
        "The Community Day clean-up on 5 July is now open for volunteers."
    ),
    "discussions": [
        {"topic": "Wellness Workshop volunteer roster", "category": "Volunteering",
         "summary": "Five volunteers confirmed for the 28 June setup; Asha to coordinate the registration desk. "
                    "Members asked for help recruiting weekend volunteers.",
         "participants": ["Asha R.", "Ben T.", "Meena S.", "Suresh P."], "participant_count": 4,
         "groups": ["Riverside Community Volunteers"], "message_count": 14,
         "links": ["https://riverside-centre.org/signup"]},
        {"topic": "New running route plan", "category": "Discussion",
         "summary": "A recurring debate over the new route was resolved after referencing section 3 of the "
                    "updated plan; the route is now finalised.",
         "participants": ["Vikram J.", "Nisha K.", "Arjun M."], "participant_count": 3,
         "groups": ["City Run Club Organizers"], "message_count": 9, "links": []},
        {"topic": "Trial class-fee policy", "category": "Announcement",
         "summary": "Proposal to introduce a small class fee was debated respectfully and agreed for a "
                    "one-month trial starting in July.",
         "participants": ["Lakshmi V.", "Kiran D.", "Deepa N."], "participant_count": 3,
         "groups": ["Downtown Wellness Group"], "message_count": 8, "links": []},
        {"topic": "Nutrition talk recording", "category": "Knowledge",
         "summary": "Recording of last week's nutrition talk was requested and shared.",
         "participants": ["Kiran D.", "Deepa N."], "participant_count": 2,
         "groups": ["Downtown Wellness Group"], "message_count": 4,
         "links": ["https://example.org/nutrition-talk"]},
        {"topic": "Pacer mentors for new runners", "category": "Volunteering",
         "summary": "Three pacers requested to mentor beginners at upcoming sessions.",
         "participants": ["Arjun M.", "Nisha K."], "participant_count": 2,
         "groups": ["City Run Club Organizers"], "message_count": 5, "links": []},
        {"topic": "First-aid refresher feedback", "category": "Discussion",
         "summary": "Members praised last Saturday's first-aid refresher and suggested repeating it quarterly.",
         "participants": ["Anita G.", "Ramesh K."], "participant_count": 2,
         "groups": ["Riverside Community Volunteers"], "message_count": 6, "links": []},
    ],
    "events": [
        {"title": "Wellness Workshop (Beginners)", "date": "28 June 2025", "time": "6:00 PM",
         "venue": "Riverside Community Hall", "location": "12 Riverside Rd",
         "conference_link": "", "registration_link": "https://riverside-centre.org/signup",
         "contact": "Priya, 555-0142", "host": "Kaushani D.",
         "group": "Riverside Community Volunteers", "status": "Upcoming", "_sort": "2025-06-28"},
        {"title": "Beginners Training Session", "date": "30 June 2025", "time": "7:30 AM",
         "venue": "Civic Auditorium", "location": "Downtown",
         "conference_link": "", "registration_link": "", "contact": "Arjun M.", "host": "",
         "group": "City Run Club Organizers", "status": "Upcoming", "_sort": "2025-06-30"},
        {"title": "Community Day Clean-Up", "date": "5 July 2025", "time": "9:00 AM",
         "venue": "Central Park", "location": "", "conference_link": "",
         "registration_link": "", "contact": "Suresh P.", "host": "",
         "group": "Riverside Community Volunteers", "status": "Upcoming", "_sort": "2025-07-05"},
        {"title": "Nutrition Talk (online)", "date": "12 June 2025", "time": "5:00 PM",
         "venue": "", "location": "Online", "conference_link": "https://meet.google.com/abc-defg-hij",
         "registration_link": "", "contact": "", "host": "Dr. Lee",
         "group": "Downtown Wellness Group", "status": "Past", "_sort": "2025-06-12"},
    ],
    "pleasantries": {
        "count": 23, "by_type": {"thanks": 11, "birthday": 7, "congratulations": 3, "good morning": 2},
        "top_people": [["Anita G.", 4], ["Ben T.", 3], ["Lakshmi V.", 3], ["Meena S.", 2]],
    },
    "low_activity_groups": [{"group": "Neighbourhood Notices", "messages": 2}],
}


def main():
    cfg = load_config()
    cfg["report"]["title"] = "Riverside Community — WhatsApp Digest"
    cfg["report"]["subtitle"] = "Sample Report · Synthetic Data"
    cfg["report"]["organisation"] = "Riverside Community"

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("report.html.j2")
    html = tpl.render(**report_mod.build_context(cfg, FAKE))
    out_html = SAMPLES / "sample_report.html"
    out_html.write_text(html, encoding="utf-8")
    print("wrote", out_html)

    out_pdf = SAMPLES / "sample_report.pdf"
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.goto(out_html.resolve().as_uri(), wait_until="networkidle")
        page.pdf(path=str(out_pdf), format="A4", print_background=True,
                 display_header_footer=True, header_template="<div></div>",
                 footer_template=render_pdf._footer(cfg),
                 margin={"top": "6mm", "bottom": "11mm", "left": "0mm", "right": "0mm"})
        b.close()
    print("wrote", out_pdf)


if __name__ == "__main__":
    main()
