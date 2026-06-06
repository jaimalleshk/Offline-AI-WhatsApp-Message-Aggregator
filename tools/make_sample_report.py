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

# ── a fully fake analysis payload (matches analyze.py's schema) ───────────────
trend = []
counts = [4, 9, 6, 2, 1, 12, 15, 7, 5, 8, 3, 2, 10, 14, 6, 4, 9, 7, 3, 11, 16]
d = START
for c in counts:
    trend.append({"date": d.isoformat(), "count": c})
    d += dt.timedelta(days=1)

FAKE = {
    "window": {"start": START.isoformat(), "end": END.isoformat()},
    "generated_at": dt.datetime.now().isoformat(),
    "dedup": {"total_considered": 168, "unique_messages": 142,
              "duplicate_copies_removed": 26, "cross_group_duplicate_topics": 5},
    "overall_health": {"avg_positivity": 74, "avg_argumentativeness": 21},
    "executive_summary": (
        "Community activity was healthy and collaborative across the three groups this period, "
        "with 142 unique messages from 18 members. Conversation centred on the upcoming Wellness "
        "Workshop and the Community Day clean-up, both of which drew strong volunteer sign-ups. "
        "Sentiment was largely positive; the only friction was a brief, respectful disagreement "
        "over the new event-fee policy, which resolved constructively. Five topics were forwarded "
        "across multiple groups and de-duplicated for this report."
    ),
    "stats": {
        "total_unique": 142, "active_participants": 18, "groups_count": 3,
        "avg_per_day": 6.8, "images_shared": 19, "links_shared": 11, "questions_asked": 23,
        "per_category": [["Events", 41], ["Volunteering", 33], ["Discussions", 28],
                         ["Knowledge", 22], ["Announcements", 12], ["Other", 6]],
        "per_group": [["Riverside Community Volunteers", 64],
                      ["Downtown Wellness Group", 47],
                      ["City Run Club Organizers", 31]],
        "sentiment": {"positive": 78, "neutral": 51, "negative": 13, "net_score": 45.8},
        "top_contributors": [["Asha R.", 21], ["Ben T.", 18], ["Carlos M.", 16],
                             ["Dana K.", 14], ["Evan L.", 12], ["Farah S.", 11],
                             ["Gita P.", 9], ["Hassan Q.", 8], ["Ivy W.", 7], ["Jordan N.", 6]],
        "trend": trend,
        "busiest_day": {"date": "2025-06-21", "count": 16},
    },
    "groups": {
        "Riverside Community Volunteers": {
            "summary": ("The busiest group, focused on organising the Community Day clean-up and the "
                        "Wellness Workshop. Coordination was smooth and members were quick to volunteer "
                        "for setup and registration roles."),
            "highlights": ["5 volunteers confirmed for the June 14 setup",
                           "First-aid refresher praised by attendees",
                           "Discussion on recruiting weekend volunteers"],
            "health_label": "Healthy", "positivity": 79, "argumentativeness": 14,
            "repeated_topics": ["Community Day logistics"], "message_count": 64,
            "notes": "high collaboration",
        },
        "Downtown Wellness Group": {
            "summary": ("Supportive, knowledge-sharing tone with daily wellness tips and a shared "
                        "recording of the nutrition talk. A short debate on introducing a small class "
                        "fee stayed civil and ended in agreement to trial it."),
            "highlights": ["Nutrition talk recording shared",
                           "Civil debate on class-fee policy",
                           "Morning-stretch tip thread"],
            "health_label": "Mixed", "positivity": 71, "argumentativeness": 28,
            "repeated_topics": [], "message_count": 47, "notes": "one resolved disagreement",
        },
        "City Run Club Organizers": {
            "summary": ("Planning the Beginners Training Session and finalising the new running route. "
                        "A recurring route debate resurfaced but was settled after referencing the "
                        "updated plan."),
            "highlights": ["Beginners Training Session scheduled",
                           "Route plan finalised", "Pacer mentors requested"],
            "health_label": "Differing Opinions", "positivity": 68, "argumentativeness": 33,
            "repeated_topics": ["new route plan"], "message_count": 31, "notes": "",
        },
    },
    "events": [
        {"title": "Wellness Workshop (Beginners)", "date": "2025-06-28", "time": "6:00 PM",
         "venue": "Riverside Community Hall", "group": "Riverside Community Volunteers",
         "status": "Upcoming", "_sort": "2025-06-28"},
        {"title": "Community Day Clean-Up", "date": "2025-07-05", "time": "9:00 AM",
         "venue": "Central Park", "group": "Riverside Community Volunteers",
         "status": "Upcoming", "_sort": "2025-07-05"},
        {"title": "Beginners Training Session", "date": "2025-06-30", "time": "7:30 AM",
         "venue": "Civic Auditorium", "group": "City Run Club Organizers",
         "status": "Upcoming", "_sort": "2025-06-30"},
        {"title": "Monthly Members Meetup", "date": "", "time": "", "venue": "TBD",
         "group": "Downtown Wellness Group", "status": "Undated", "_sort": ""},
        {"title": "Nutrition Talk", "date": "2025-06-12", "time": "5:00 PM",
         "venue": "Online", "group": "Downtown Wellness Group", "status": "Past", "_sort": "2025-06-12"},
        {"title": "Spring Community Walk", "date": "2025-06-07", "time": "8:00 AM",
         "venue": "Riverside Trail", "group": "Riverside Community Volunteers",
         "status": "Past", "_sort": "2025-06-07"},
    ],
}


def main():
    cfg = load_config()
    cfg["report"]["title"] = "Riverside Community — WhatsApp Intelligence Report"
    cfg["report"]["subtitle"] = "Sample Report · Synthetic Data"
    cfg["report"]["organisation"] = "Riverside Community"
    cfg["keywords"] = ["Community", "Wellness", "Run Club"]

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
