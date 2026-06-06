"""Render analysis.json into a compact, professional HTML report."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import LOG, ROOT, load_config, p, read_json

TEMPLATES = ROOT / "templates"


def fmt_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).strftime("%d %b %Y")
    except Exception:
        return s


def build_context(cfg: dict, a: dict) -> dict:
    stats = a["stats"]
    trend = stats["trend"]
    max_t = max((t["count"] for t in trend), default=1) or 1
    trend_bars = [{"date": t["date"], "count": t["count"],
                   "pct": round(t["count"] / max_t * 100),
                   "label": dt.date.fromisoformat(t["date"]).strftime("%d/%m"),
                   "dow": dt.date.fromisoformat(t["date"]).strftime("%a")} for t in trend]

    cat_total = sum(c for _, c in stats["per_category"]) or 1
    cats = [{"name": n, "count": c, "pct": round(c / cat_total * 100)}
            for n, c in stats["per_category"]]

    grp_total = sum(c for _, c in stats["per_group"]) or 1
    groups_bar = [{"name": n, "count": c, "pct": round(c / grp_total * 100)}
                  for n, c in stats["per_group"]]

    s = stats["sentiment"]
    s_total = (s["positive"] + s["neutral"] + s["negative"]) or 1
    sentiment = {
        "positive": s["positive"], "neutral": s["neutral"], "negative": s["negative"],
        "pos_pct": round(s["positive"] / s_total * 100),
        "neu_pct": round(s["neutral"] / s_total * 100),
        "neg_pct": round(s["negative"] / s_total * 100),
        "net": s["net_score"],
    }

    events = a.get("events", [])
    ev_groups = {"Upcoming": [], "Undated": [], "Past": []}
    for e in events:
        ev_groups.setdefault(e["status"], []).append(e)

    # ── events calendar: upcoming (soonest first) → undated → past (most recent
    #    first, shown last), each with a coloured date badge ──
    def _badge(e):
        s = e.get("_sort") or ""
        if s:
            try:
                d = dt.date.fromisoformat(s)
                return {"num": str(d.day), "mon": d.strftime("%b")}
            except Exception:
                pass
        return {"num": "TBD", "mon": ""}

    up = sorted(ev_groups["Upcoming"], key=lambda e: e.get("_sort") or "9999")
    und = ev_groups["Undated"]
    past = sorted(ev_groups["Past"], key=lambda e: e.get("_sort") or "0000", reverse=True)
    palette = ["", "green", "teal", "orange", "coral", "amber"]
    events_cal, ci = [], 0
    for e in (up + und + past):
        ee = dict(e)
        ee["badge"] = _badge(e)
        if e["status"] == "Past":
            ee["badge_cls"] = "gray"
        elif e["status"] == "Undated":
            ee["badge_cls"] = "amber"
        else:
            ee["badge_cls"] = palette[ci % len(palette)]; ci += 1
        events_cal.append(ee)

    dd = a.get("dedup", {})
    net = sentiment["net"]
    kpis = [
        {"v": stats["total_unique"], "l": "Unique messages"},
        {"v": stats["active_participants"], "l": "Participants"},
        {"v": stats["groups_count"], "l": "Groups"},
        {"v": len(events), "l": "Events found"},
        {"v": stats["images_shared"], "l": "Images read"},
        {"v": dd.get("duplicate_copies_removed", 0), "l": "Duplicates removed"},
    ]
    mood = "\U0001F642" if net >= 0 else "\U0001F61F"
    sign = "+" if net >= 0 else ""
    hero_pills = [
        f"\U0001F4AC {stats['total_unique']} unique messages",
        f"\U0001F465 {stats['active_participants']} participants",
        f"\U0001F4C5 {len(events)} events",
        f"{mood} Net sentiment {sign}{net}",
    ]
    org = cfg["report"]["organisation"]
    org_initials = "".join(w[0] for w in org.split()[:3]).upper() or "WA"

    # group cards enriched
    gcards = []
    for name, g in a.get("groups", {}).items():
        gcards.append({
            "name": name,
            "summary": g.get("summary", ""),
            "highlights": g.get("highlights", []),
            "health": g.get("health_label", "—"),
            "positivity": g.get("positivity", 0),
            "argumentativeness": g.get("argumentativeness", 0),
            "repeated": g.get("repeated_topics", []),
            "count": g.get("message_count", 0),
            "notes": g.get("notes", ""),
        })
    gcards.sort(key=lambda x: x["count"], reverse=True)

    # which sections to render (agent/intent can narrow this); defaults to all
    all_sections = ["trend", "sentiment", "categories", "groupvol",
                    "participation", "group_digests", "events"]
    include = (cfg.get("report", {}) or {}).get("include") or all_sections

    return {
        "cfg": cfg,
        "include": include,
        "focus": (cfg.get("report", {}) or {}).get("focus", ""),
        "title": cfg["report"]["title"],
        "subtitle": cfg["report"]["subtitle"],
        "org": cfg["report"]["organisation"],
        "window_start": fmt_date(a["window"]["start"]),
        "window_end": fmt_date(a["window"]["end"]),
        "period_days": (dt.date.fromisoformat(a["window"]["end"])
                        - dt.date.fromisoformat(a["window"]["start"])).days,
        "generated": dt.datetime.fromisoformat(a["generated_at"]).strftime("%d %b %Y, %H:%M"),
        "stats": stats,
        "dedup": a["dedup"],
        "trend_bars": trend_bars,
        "cats": cats,
        "groups_bar": groups_bar,
        "sentiment": sentiment,
        "overall_health": a.get("overall_health", {}),
        "exec_summary": a.get("executive_summary", ""),
        "events": ev_groups,
        "events_cal": events_cal,
        "event_total": len(events),
        "kpis": kpis,
        "hero_pills": hero_pills,
        "org_initials": org_initials,
        "gcards": gcards,
        "keywords": cfg["keywords"],
        "busiest": (lambda b: {"date": fmt_date(b["date"]), "count": b["count"]} if b else None)(stats.get("busiest_day")),
    }


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
