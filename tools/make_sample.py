"""Generate generic, brand-neutral sample data to exercise the pipeline without
live scraping.

Creates group JSON files (matching the scraper's output schema) plus a rendered
event-poster PNG, so OCR -> analyze -> report -> pdf can be validated end to end.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from common import load_config, p, slug, write_json  # noqa: E402

cfg = load_config()
end = dt.date(2026, 6, 6)


def D(days_ago, hh=10, mm=0):
    return (dt.datetime(end.year, end.month, end.day, hh, mm) - dt.timedelta(days=days_ago)).isoformat()


# ── build an event poster image so OCR/vision has something real to read ─────
def make_poster() -> str:
    media = p(cfg, "media")
    img = Image.new("RGB", (700, 900), "#0f4c5c")
    d = ImageDraw.Draw(img)

    def font(sz, bold=True):
        for name in (("arialbd.ttf" if bold else "arial.ttf"), "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, sz)
            except Exception:
                continue
        return ImageFont.load_default()

    d.rectangle([0, 0, 700, 150], fill="#e08a1e")
    d.text((40, 40), "COMMUNITY CENTRE", font=font(46), fill="#ffffff")
    d.text((40, 200), "Wellness Workshop", font=font(46), fill="#ffe9cc")
    d.text((40, 300), "Beginners welcome", font=font(30, False), fill="#cdeaf2")
    d.text((40, 430), "Date: 14 June 2026", font=font(34), fill="#ffffff")
    d.text((40, 490), "Time: 6:00 PM - 8:30 PM", font=font(34), fill="#ffffff")
    d.text((40, 550), "Venue: Riverside Community Hall", font=font(30), fill="#ffffff")
    d.text((40, 650), "Register: riverside-centre.org/signup", font=font(26, False), fill="#ffe9cc")
    d.text((40, 720), "Contact: Priya 555-0142", font=font(26, False), fill="#ffe9cc")
    d.text((40, 820), "Free entry - All are welcome", font=font(28), fill="#1a8aa6")
    path = media / "sample_poster_event.png"
    img.save(path)
    return str((Path(cfg["paths"]["media"]) / path.name).as_posix())


poster = make_poster()

# A message forwarded across multiple groups (tests cross-group de-duplication)
DUP = ("Forwarding: Our annual community day is coming up. Please block your calendars "
       "and invite family and neighbours. More details soon!")

groups = {
    "Riverside Community Volunteers": [
        ("Priya", D(20), "Welcome everyone to the Riverside volunteers group!", []),
        ("Ramesh", D(18), "Wellness Workshop poster for next week - please share widely.", [poster]),
        ("Priya", D(18, 11), DUP, []),
        ("Anita", D(15), "The first-aid refresher last Saturday was excellent, learned a lot.", []),
        ("Ramesh", D(14), "Question: what's the best way to recruit more weekend volunteers? Any tips?", []),
        ("Anita", D(14, 12), "Personal invites work best. Tip: pair new folks with an experienced buddy.", []),
        ("Suresh", D(10), "We need 5 volunteers for setup at the June 14 event. Reply if you can help.", []),
        ("Meena", D(10, 13), "I can volunteer for the registration desk!", []),
        ("Suresh", D(9), "Honestly I disagree with how the last budget was decided, felt rushed and unfair.", []),
        ("Ramesh", D(9, 10), "I understand the concern, but the committee did discuss it openly. Let's talk constructively.", []),
        ("Suresh", D(9, 11), "Fine, but next time please loop us in earlier. This keeps happening.", []),
        ("Priya", D(4), "Reminder: Wellness Workshop on 14 June, 6 PM at Riverside Community Hall. Register early!", []),
        ("Anita", D(2), "Loved today's planning session. Feeling grateful and motivated.", []),
    ],
    "Downtown Wellness Group": [
        ("Lakshmi", D(19), "Welcome all to the wellness group. Let's keep it positive and supportive.", []),
        ("Lakshmi", D(18, 9), DUP, []),  # duplicate of the forwarded message
        ("Kiran", D(16), "Tip of the day: ten minutes of stretching each morning makes a big difference.", []),
        ("Deepa", D(12), "Past event note: the May 24 community walk was wonderful, thanks to all who came.", []),
        ("Kiran", D(11), "Does anyone have the recording of last week's nutrition talk?", []),
        ("Deepa", D(11, 12), "Yes, sharing the link: http://example.org/nutrition-talk", []),
        ("Lakshmi", D(6), "Upcoming: Community Day celebration on 21 June, 5 PM, Central Park.", []),
        ("Kiran", D(3), "Such a relaxing session today, the breathing exercises were great.", []),
    ],
    "City Run Club Organizers": [
        ("Arjun", D(20), "Run club coordination group. Share schedules and questions here.", []),
        ("Arjun", D(17), DUP, []),  # duplicate again -> appears in 3 groups
        ("Nisha", D(15), "The May 30 trial run went well (past). Feedback was encouraging.", []),
        ("Vikram", D(13), "I think the new route plan is confusing and contradicts the old one. Frustrating.", []),
        ("Nisha", D(13, 11), "It does differ, but I find it clearer once you read section 3. Different styles I guess.", []),
        ("Vikram", D(13, 12), "We keep rehashing this route debate every week, can we just finalize it?", []),
        ("Arjun", D(8), "Volunteers needed: 3 pacers to mentor new runners. Great way to help out!", []),
        ("Nisha", D(5), "Upcoming: Beginners Training Session on 28 June at the Civic Auditorium.", []),
        ("Vikram", D(1), "Great run today, learned a lot about pacing. Thanks team!", []),
    ],
}

raw = p(cfg, "raw")
index = []
for g, rows in groups.items():
    msgs = []
    for sender, ts, text, media in rows:
        msgs.append({"group": g, "sender": sender, "timestamp": ts, "text": text,
                     "media": media, "has_image": bool(media)})
    doc = {"group": g, "scraped_at": dt.datetime.now().isoformat(),
           "message_count": len(msgs), "messages": msgs}
    write_json(raw / f"{slug(g)}.json", doc)
    index.append({"group": g, "file": f"{slug(g)}.json", "messages": len(msgs)})
    print(f"wrote {g}: {len(msgs)} messages")

write_json(raw / "_index.json", {"window": ["2026-05-16", "2026-06-06"], "groups": index})
print("Sample data ready. Poster:", poster)
