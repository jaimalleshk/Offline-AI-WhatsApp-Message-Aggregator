"""Render analysis.json into the fixed, professional HTML report."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import LOG, ROOT, load_config, p, read_json

TEMPLATES = ROOT / "templates"
CAT_COLORS = {  # category badge tints
    "Event": "blue", "Announcement": "purple", "Knowledge": "teal",
    "Volunteering": "green", "Question": "amber", "Logistics": "orange",
    "Discussion": "gray",
}


def fmt_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).strftime("%d %b %Y")
    except Exception:
        return s


def _badge(sort: str) -> dict:
    if sort:
        try:
            d = dt.date.fromisoformat(sort)
            return {"num": str(d.day), "mon": d.strftime("%b")}
        except Exception:
            pass
    return {"num": "TBD", "mon": ""}


def build_context(cfg: dict, a: dict) -> dict:
    stats = a.get("stats", {})
    dd = a.get("dedup", {})

    kpis = [
        {"v": stats.get("total_unique", 0), "l": "Unique messages"},
        {"v": stats.get("discussions_count", 0), "l": "Discussions"},
        {"v": stats.get("events_count", 0), "l": "Events"},
        {"v": stats.get("active_participants", 0), "l": "Participants"},
        {"v": stats.get("groups_count", 0), "l": "Groups"},
        {"v": dd.get("duplicate_copies_removed", 0), "l": "Duplicates removed"},
    ]

    # discussions: attach a colour per category
    discussions = []
    for d in a.get("discussions", []):
        dc = dict(d)
        dc["color"] = CAT_COLORS.get(d.get("category", "Discussion"), "gray")
        discussions.append(dc)

    # events: ordered already (upcoming → undated → past); attach date badges
    events, ci, palette = [], 0, ["", "green", "teal", "orange", "coral", "amber"]
    for e in a.get("events", []):
        ee = dict(e)
        ee["badge"] = _badge(e.get("_sort", ""))
        ee["badge_cls"] = ("gray" if e["status"] == "Past"
                           else "amber" if e["status"] == "Undated"
                           else palette[ci % len(palette)])
        if e["status"] == "Upcoming":
            ci += 1
        events.append(ee)

    org = cfg["report"]["organisation"]
    win = a.get("window", {})
    return {
        "cfg": cfg,
        "title": cfg["report"]["title"],
        "subtitle": cfg["report"]["subtitle"],
        "org": org,
        "org_initials": "".join(w[0] for w in org.split()[:3]).upper() or "WA",
        "window_start": fmt_date(win.get("start", "")),
        "window_end": fmt_date(win.get("end", "")),
        "period_days": _days(win),
        "generated": _gen(a.get("generated_at")),
        "criteria": a.get("criteria", {}).get("capture", []),
        "groups_covered": a.get("groups_covered", []),
        "kpis": kpis,
        "stats": stats,
        "dedup": dd,
        "highlights": a.get("highlights", ""),
        "discussions": discussions,
        "events": events,
        "event_total": len(events),
        "pleasantries": a.get("pleasantries", {}),
        "low_activity": a.get("low_activity_groups", []),
    }


def _days(win: dict) -> int:
    try:
        return (dt.date.fromisoformat(win["end"]) - dt.date.fromisoformat(win["start"])).days
    except Exception:
        return 0


def _gen(s) -> str:
    try:
        return dt.datetime.fromisoformat(s).strftime("%d %b %Y, %H:%M")
    except Exception:
        return ""


def run(cfg: dict) -> Path:
    a = read_json(p(cfg, "processed") / "analysis.json")
    if not a:
        raise SystemExit("analysis.json missing — run analyze.py first.")
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                      autoescape=select_autoescape(["html"]))
    tpl = env.get_template("report.html.j2")
    html = tpl.render(**build_context(cfg, a))
    out = p(cfg, "output") / "report.html"
    out.write_text(html, encoding="utf-8")
    LOG.info("HTML report -> %s", out)
    return out


if __name__ == "__main__":
    run(load_config())
