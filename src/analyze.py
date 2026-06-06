"""Analyse scraped WhatsApp data: de-duplicate across groups, categorise, score
sentiment & discussion health, extract events, and compute the quantitative
statistics that lead the report.

Everything is produced by the local models (qwen2.5:7b-instruct + nomic-embed)
and written to data/processed/analysis.json for the report stage.
"""
from __future__ import annotations

import collections
import datetime as dt
import re
from pathlib import Path

import numpy as np
from dateutil import parser as dtparse

from common import (LOG, Ollama, date_window, load_config, p, read_json,
                    write_json)

CATS = ["Events", "Knowledge", "Volunteering", "Discussions", "Announcements", "Other"]


# ── load + merge image text ─────────────────────────────────────────────────
def load_messages(cfg: dict) -> list[dict]:
    raw_dir = p(cfg, "raw")
    img_text = read_json(p(cfg, "processed") / "image_text.json", default={}) or {}
    msgs = []
    for f in sorted(raw_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        doc = read_json(f)
        for m in doc.get("messages", []):
            extra = []
            for mp in m.get("media", []):
                info = img_text.get(mp)
                if info and info.get("final_text"):
                    extra.append(info["final_text"])
                    m["image_is_event"] = m.get("image_is_event") or info.get("is_event_poster")
            m["image_text"] = "\n".join(extra)
            m["content"] = (m.get("text", "") + ("\n" + m["image_text"] if m["image_text"] else "")).strip()
            if m["content"] or m.get("has_image"):
                msgs.append(m)
    LOG.info("Loaded %d messages across %d group files", len(msgs), len(list(raw_dir.glob('*.json'))))
    return msgs


# ── cross-group de-duplication via embeddings ───────────────────────────────
def dedupe(cfg: dict, msgs: list[dict]) -> dict:
    o = Ollama(cfg)
    thr = cfg["dedup"]["similarity_threshold"]
    min_chars = cfg["dedup"]["min_chars"]
    cache = read_json(p(cfg, "processed") / "embeddings.json", default={}) or {}

    vecs, idxs = [], []
    for i, m in enumerate(msgs):
        c = m["content"]
        if len(c) < min_chars:
            continue
        key = c[:200]
        if key not in cache:
            try:
                cache[key] = o.embed(c[:1000])
            except Exception as e:
                LOG.warning("embed failed: %s", e)
                continue
        vecs.append(np.array(cache[key], dtype=np.float32))
        idxs.append(i)
    write_json(p(cfg, "processed") / "embeddings.json", cache)

    for m in msgs:
        m["cluster"] = None
    clusters: list[dict] = []  # {centroid, members:[idx]}
    if vecs:
        mat = np.vstack(vecs)
        mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        for k, gi in enumerate(idxs):
            v = mat[k]
            best, best_sim = -1, 0.0
            for ci, cl in enumerate(clusters):
                sim = float(np.dot(v, cl["centroid"]))
                if sim > best_sim:
                    best, best_sim = ci, sim
            if best_sim >= thr:
                cl = clusters[best]
                cl["members"].append(gi)
                n = len(cl["members"])
                cl["centroid"] = (cl["centroid"] * (n - 1) + v) / n
                cl["centroid"] /= (np.linalg.norm(cl["centroid"]) + 1e-9)
                msgs[gi]["cluster"] = best
            else:
                clusters.append({"centroid": v.copy(), "members": [gi]})
                msgs[gi]["cluster"] = len(clusters) - 1

    # decide canonical message per cluster (earliest), flag duplicates
    cross_group_clusters = 0
    dup_copies = 0
    unique_idx = set(range(len(msgs)))  # start with all; short msgs stay unique
    for ci, cl in enumerate(clusters):
        members = cl["members"]
        groups = {msgs[i]["group"] for i in members}
        members_sorted = sorted(members, key=lambda i: msgs[i]["timestamp"] or "")
        canonical = members_sorted[0]
        for i in members_sorted[1:]:
            msgs[i]["is_duplicate"] = True
            unique_idx.discard(i)
            dup_copies += 1
        msgs[canonical]["dup_in_groups"] = sorted(groups)
        msgs[canonical]["dup_count"] = len(members)
        if len(groups) > 1:
            cross_group_clusters += 1
    for i in range(len(msgs)):
        msgs[i].setdefault("is_duplicate", False)

    stats = {
        "total_considered": len(msgs),
        "unique_messages": len(unique_idx),
        "duplicate_copies_removed": dup_copies,
        "cross_group_duplicate_topics": cross_group_clusters,
    }
    LOG.info("Dedup: %d total -> %d unique (%d duplicate copies, %d cross-group topics)",
             stats["total_considered"], stats["unique_messages"],
             stats["duplicate_copies_removed"], stats["cross_group_duplicate_topics"])
    return {"unique_idx": sorted(unique_idx), "stats": stats}


# ── per-message classification (category, sentiment, event) ─────────────────
CLASSIFY_SYS = (
    "You classify messages from community WhatsApp groups. "
    "Categories: Events (gatherings/courses/meetups with logistics like date/time/venue), "
    "Knowledge (information, tips, teachings, Q&A), "
    "Volunteering (help requests/offers, service or logistics coordination), "
    "Discussions (opinions, debate, general chat), "
    "Announcements (notices, updates, info broadcasts), Other. "
    "Sentiment is one of: positive, neutral, negative."
)


def classify(cfg: dict, msgs: list[dict], idxs: list[int]) -> None:
    o = Ollama(cfg)
    batch = 8
    todo = [i for i in idxs if not msgs[i].get("_classified")]
    LOG.info("Classifying %d unique messages...", len(todo))
    for s in range(0, len(todo), batch):
        chunk = todo[s:s + batch]
        items = []
        for n, i in enumerate(chunk):
            items.append(f'#{n}: """{msgs[i]["content"][:500]}"""')
        prompt = (
            "Classify each message. Return a JSON object with key \"items\": a list where "
            "each element is {\"id\": <int>, \"category\": <one category>, "
            "\"sentiment\": <positive|neutral|negative>, \"is_event\": <bool>, "
            "\"event\": {\"title\":\"\",\"date\":\"\",\"time\":\"\",\"venue\":\"\"} or null, "
            "\"topic\": <<=6 word topic>}.\n\nMessages:\n" + "\n".join(items)
        )
        try:
            res = o.chat_json(prompt, system=CLASSIFY_SYS)
            arr = res.get("items", res if isinstance(res, list) else [])
        except Exception as e:
            LOG.warning("classify batch failed: %s", e)
            arr = []
        by_id = {int(x.get("id", -1)): x for x in arr if isinstance(x, dict)}
        for n, i in enumerate(chunk):
            x = by_id.get(n, {})
            cat = _as_str(x.get("category")) or "Other"
            msgs[i]["category"] = cat if cat in CATS else "Other"
            sent = (_as_str(x.get("sentiment")) or "neutral").lower()
            msgs[i]["sentiment"] = sent if sent in ("positive", "neutral", "negative") else "neutral"
            msgs[i]["topic"] = _as_str(x.get("topic"))
            ev = x.get("event")
            msgs[i]["is_event"] = bool(x.get("is_event")) or msgs[i].get("image_is_event", False)
            msgs[i]["event"] = ev if isinstance(ev, dict) else None
            msgs[i]["_classified"] = True
        LOG.info("  classified %d/%d", min(s + batch, len(todo)), len(todo))


# ── per-group summary + discussion health ───────────────────────────────────
HEALTH_SYS = (
    "You assess the conversational health of a WhatsApp group over a period. "
    "Be precise and base scores on the evidence provided."
)


def group_summaries(cfg: dict, msgs: list[dict], unique_idx: list[int]) -> dict:
    o = Ollama(cfg)
    by_group: dict[str, list[int]] = collections.defaultdict(list)
    for i in unique_idx:
        by_group[msgs[i]["group"]].append(i)
    out = {}
    for g, idxs in by_group.items():
        idxs = sorted(idxs, key=lambda i: msgs[i]["timestamp"] or "")
        convo = "\n".join(f"{msgs[i]['sender']}: {msgs[i]['content'][:200]}" for i in idxs[:120])
        prompt = (
            f"Group: {g}\nMessages (sample):\n{convo[:6000]}\n\n"
            "Return JSON: {\"summary\": <=60 words, \"highlights\": [up to 4 short bullets], "
            "\"health_label\": one of [Healthy, Positive, Mostly Positive, Mixed, "
            "Differing Opinions, Argumentative], \"positivity\": 0-100, "
            "\"argumentativeness\": 0-100, \"repeated_topics\": [short strings], "
            "\"notes\": <=25 words}."
        )
        try:
            res = o.chat_json(prompt, system=HEALTH_SYS)
        except Exception as e:
            LOG.warning("group summary failed for %s: %s", g, e)
            res = {}
        res["message_count"] = len(idxs)
        out[g] = res
        LOG.info("  summarised group: %s (%d msgs, %s)", g, len(idxs),
                 res.get("health_label", "?"))
    return out


# ── events ──────────────────────────────────────────────────────────────────
def _as_str(v) -> str:
    """Coerce any LLM-returned value (str/list/number/None) to a stripped string."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(_as_str(x) for x in v).strip()
    if isinstance(v, dict):
        return " ".join(_as_str(x) for x in v.values()).strip()
    return str(v).strip()


def collect_events(cfg: dict, msgs: list[dict], unique_idx: list[int], today: dt.date) -> list[dict]:
    events = []
    for i in unique_idx:
        m = msgs[i]
        if not m.get("is_event"):
            continue
        ev = m.get("event") if isinstance(m.get("event"), dict) else {}
        title = _as_str(ev.get("title")) or _as_str(m.get("topic")) or _as_str(m.get("content"))[:60]
        date_str = _as_str(ev.get("date"))
        when, status = None, "Undated"
        if date_str:
            for dayfirst in (True, False):
                try:
                    when = dtparse.parse(date_str, dayfirst=dayfirst, default=dt.datetime(today.year, 1, 1))
                    break
                except Exception:
                    continue
        if when:
            status = "Past" if when.date() < today else "Upcoming"
        events.append({
            "title": title, "date": date_str, "time": _as_str(ev.get("time")),
            "venue": _as_str(ev.get("venue")), "group": m["group"],
            "status": status, "_sort": when.date().isoformat() if when else "",
            "_when": when.date() if when else None,
        })
    # merge near-duplicate events (same poster + reminder messages). Key on the
    # resolved date plus the most informative fields; keep the richest record.
    merged: dict[str, dict] = {}
    for e in events:
        stop = {"the", "a", "an", "reminder", "join", "us", "for", "on", "at", "event",
                "with", "and", "of", "to", "this", "next", "upcoming", "please"}
        toks = [t for t in re.findall(r"[a-z0-9]+", e["title"].lower())
                if t not in stop and len(t) > 2]
        key = (e["_sort"] or "?") + "|" + "_".join(toks[:2])
        cur = merged.get(key)
        if not cur or _richness(e) > _richness(cur):
            if cur:  # carry over any non-empty fields the richer one lacks
                for f in ("time", "venue", "date"):
                    e[f] = e[f] or cur[f]
            merged[key] = e
    events = list(merged.values())
    LOG.info("Events: merged to %d unique", len(events))
    # order: Upcoming (date asc), Undated, Past (date desc)
    order = {"Upcoming": 0, "Undated": 1, "Past": 2}
    events.sort(key=lambda e: (order[e["status"]],
                               e["_sort"] if e["status"] == "Upcoming" else "",
                               -_ord(e["_sort"]) if e["status"] == "Past" else 0))
    for e in events:
        e.pop("_when", None)  # date objects are not JSON-serialisable
    LOG.info("Collected %d events", len(events))
    return events


def _ord(d: str) -> int:
    try:
        return int(d.replace("-", ""))
    except Exception:
        return 0


def _richness(e: dict) -> int:
    """How complete an event record is — prefer the one with date/time/venue."""
    return sum(bool(e.get(f)) for f in ("date", "time", "venue")) * 2 + len(e.get("title", ""))


# ── quantitative statistics ─────────────────────────────────────────────────
def compute_stats(msgs: list[dict], unique_idx: list[int], start: dt.date, end: dt.date) -> dict:
    U = [msgs[i] for i in unique_idx]
    days = collections.Counter()
    senders = collections.Counter()
    groups = collections.Counter()
    cats = collections.Counter()
    sents = collections.Counter()
    images = links = questions = 0
    for m in U:
        ts = m.get("timestamp")
        if ts:
            days[ts[:10]] += 1
        senders[m.get("sender", "Unknown")] += 1
        groups[m["group"]] += 1
        cats[m.get("category", "Other")] += 1
        sents[m.get("sentiment", "neutral")] += 1
        images += 1 if m.get("has_image") else 0
        links += m.get("content", "").count("http")
        questions += 1 if "?" in m.get("content", "") else 0

    span_days = max(1, (end - start).days)
    # full daily trend (zero-filled)
    trend = []
    d = start
    while d <= end:
        trend.append({"date": d.isoformat(), "count": days.get(d.isoformat(), 0)})
        d += dt.timedelta(days=1)

    total = len(U)
    pos, neu, neg = sents.get("positive", 0), sents.get("neutral", 0), sents.get("negative", 0)
    sent_score = round((pos - neg) / total * 100, 1) if total else 0.0
    return {
        "total_unique": total,
        "active_participants": len([s for s in senders if s != "Unknown"]),
        "groups_count": len(groups),
        "avg_per_day": round(total / span_days, 1),
        "images_shared": images,
        "links_shared": links,
        "questions_asked": questions,
        "per_group": groups.most_common(),
        "per_category": [(c, cats.get(c, 0)) for c in CATS if cats.get(c, 0)],
        "sentiment": {"positive": pos, "neutral": neu, "negative": neg, "net_score": sent_score},
        "top_contributors": senders.most_common(10),
        "trend": trend,
        "busiest_day": max(trend, key=lambda t: t["count"]) if trend else None,
    }


def run(cfg: dict):
    start, end = date_window(cfg)
    today = end
    msgs = load_messages(cfg)
    if not msgs:
        LOG.error("No messages found. Run the scraper first.")
        return None
    dd = dedupe(cfg, msgs)
    unique_idx = dd["unique_idx"]
    classify(cfg, msgs, unique_idx)
    stats = compute_stats(msgs, unique_idx, start, end)
    groups = group_summaries(cfg, msgs, unique_idx)
    events = collect_events(cfg, msgs, unique_idx, today)

    # overall discussion-health rollup
    health_vals = [g for g in groups.values() if "positivity" in g]
    overall_health = {
        "avg_positivity": round(np.mean([g["positivity"] for g in health_vals]), 0) if health_vals else 0,
        "avg_argumentativeness": round(np.mean([g["argumentativeness"] for g in health_vals]), 0) if health_vals else 0,
    }

    # executive summary
    o = Ollama(cfg)
    gsum = "\n".join(f"- {g}: {v.get('summary','')}" for g, v in groups.items())
    focus = (cfg.get("report", {}) or {}).get("focus", "")
    focus_line = f"The reader is especially interested in: {focus}. " if focus else ""
    try:
        exec_summary = o.chat(
            "Write a concise, professional 4-5 sentence executive summary of community "
            f"WhatsApp activity from {start} to {end}. {focus_line}Stats: {stats['total_unique']} unique "
            f"messages, {stats['active_participants']} participants, {stats['groups_count']} groups, "
            f"sentiment net {stats['sentiment']['net_score']}, {len(events)} events. "
            f"Group notes:\n{gsum}\nReturn plain prose only, no headings.",
            system="You are a professional community analyst.")
    except Exception as e:
        LOG.warning("exec summary failed: %s", e)
        exec_summary = ""

    analysis = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "dedup": dd["stats"],
        "stats": stats,
        "overall_health": overall_health,
        "groups": groups,
        "events": events,
        "executive_summary": exec_summary,
        "generated_at": dt.datetime.now().isoformat(),
    }
    write_json(p(cfg, "processed") / "analysis.json", analysis)
    LOG.info("Analysis written -> %s", p(cfg, 'processed') / 'analysis.json')
    return analysis


def main():
    run(load_config())


if __name__ == "__main__":
    main()
