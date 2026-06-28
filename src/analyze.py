"""Analyse scraped WhatsApp data into a compact, de-duplicated digest.

Pipeline (all local, offline):
  1. load + merge OCR/vision text for each message
  2. clean: drop WhatsApp system notices; route greetings/thanks/birthdays to a
     low-value "pleasantries" bucket (kept, but reported last)
  3. de-duplicate across groups (embeddings) — the "compaction" step
  4. extract every UNIQUE DISCUSSION (topic + factual summary + who took part)
  5. extract every EVENT with full logistics (date/time/venue/location/links/host)
  6. write data/processed/analysis.json  (events also exported separately)

What is extracted is driven by config.yaml -> `extraction` (not hard-coded).
"""
from __future__ import annotations

import collections
import datetime as dt
import re

import numpy as np
from dateutil import parser as dtparse

from common import LOG, Ollama, date_window, load_config, p, read_json, write_json
import progress

URL_RE = re.compile(r"https?://[^\s)>\]\"']+", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────
def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(_as_str(x) for x in v).strip()
    if isinstance(v, dict):
        return " ".join(_as_str(x) for x in v.values()).strip()
    return str(v).strip()


def _as_list(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [s for s in (_as_str(x) for x in v) if s]
    s = _as_str(v)
    return [s] if s else []


def _chunks(items, max_chars=4500, max_msgs=45):
    cur, n = [], 0
    for m in items:
        c = len(m.get("content", "")) + 20
        if cur and (n + c > max_chars or len(cur) >= max_msgs):
            yield cur
            cur, n = [], 0
        cur.append(m); n += c
    if cur:
        yield cur


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
    LOG.info("Loaded %d messages across %d group files",
             len(msgs), len([f for f in raw_dir.glob('*.json') if not f.name.startswith('_')]))
    return msgs


# ── cleaning: drop system notices, split off pleasantries ───────────────────
def _compile(patterns):
    out = []
    for pat in patterns:
        try:
            out.append(re.compile(pat, re.I))
        except re.error:
            out.append(re.compile(re.escape(pat), re.I))
    return out


def clean(cfg: dict, msgs: list[dict]):
    ex = cfg.get("extraction", {}) or {}
    sys_re = _compile(ex.get("system_message_patterns", [])) if ex.get("drop_system_messages", True) else []
    pk = [k.lower() for k in ex.get("pleasantry_keywords", [])]
    kept, pleasantries, dropped = [], [], 0
    for m in msgs:
        c = (m.get("content") or "").strip()
        if not c:
            if m.get("has_image"):
                kept.append(m)
            continue
        low = c.lower()
        if any(rx.search(c) for rx in sys_re):
            dropped += 1
            continue
        words = re.sub(r"[^\w\s]", " ", low).split()
        is_short = len(words) <= 8 or len(c) <= 60
        if is_short and not URL_RE.search(c) and any(k in low for k in pk):
            m["_pleasantry_type"] = next((k for k in pk if k in low), "greeting")
            pleasantries.append(m)
            continue
        kept.append(m)
    LOG.info("Cleaned: %d substantive, %d pleasantries, %d system/empty dropped",
             len(kept), len(pleasantries), dropped)
    return kept, pleasantries


# ── cross-group de-duplication via embeddings (compaction) ──────────────────
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
        vecs.append(np.array(cache[key], dtype=np.float32)); idxs.append(i)
    write_json(p(cfg, "processed") / "embeddings.json", cache)

    for m in msgs:
        m["is_duplicate"] = False
    clusters = []
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
                cl = clusters[best]; cl["members"].append(gi)
                n = len(cl["members"])
                cl["centroid"] = (cl["centroid"] * (n - 1) + v) / n
                cl["centroid"] /= (np.linalg.norm(cl["centroid"]) + 1e-9)
            else:
                clusters.append({"centroid": v.copy(), "members": [gi]})

    unique_idx = set(range(len(msgs)))
    dup_copies, cross = 0, 0
    for cl in clusters:
        members = sorted(cl["members"], key=lambda i: msgs[i]["timestamp"] or "")
        groups = {msgs[i]["group"] for i in members}
        for i in members[1:]:
            msgs[i]["is_duplicate"] = True
            unique_idx.discard(i); dup_copies += 1
        msgs[members[0]]["dup_in_groups"] = sorted(groups)
        if len(groups) > 1:
            cross += 1
    stats = {"total_considered": len(msgs), "unique_messages": len(unique_idx),
             "duplicate_copies_removed": dup_copies, "cross_group_duplicate_topics": cross}
    LOG.info("De-dup: %d substantive -> %d unique (%d duplicate copies, %d cross-group topics)",
             stats["total_considered"], stats["unique_messages"], dup_copies, cross)
    return {"unique": [msgs[i] for i in sorted(unique_idx)], "stats": stats}


# ── unique discussions (topic + summary + participants) ─────────────────────
DISC_SYS = ("You are a precise analyst extracting DISTINCT discussion threads from a WhatsApp "
            "group. Use ONLY information present in the messages. NEVER invent names, facts, "
            "dates, numbers, or links. Ignore greetings, thanks, and birthday wishes.")
DISC_CATS = ["Discussion", "Announcement", "Knowledge", "Volunteering", "Question", "Event", "Logistics"]


def extract_discussions(cfg: dict, unique: list[dict]) -> list[dict]:
    o = Ollama(cfg)
    min_g = cfg.get("extraction", {}).get("min_messages_per_group", 3)
    by_group = collections.defaultdict(list)
    for m in unique:
        by_group[m["group"]].append(m)

    jobs = []
    low_activity = []
    for g, gm in by_group.items():
        if len(gm) < min_g:
            low_activity.append({"group": g, "messages": len(gm)})
            continue
        gm.sort(key=lambda m: m["timestamp"] or "")
        for ch in _chunks(gm):
            jobs.append((g, gm, ch))

    raw = []
    for k, (g, gm, ch) in enumerate(jobs):
        senders = sorted({m["sender"] for m in gm if m.get("sender") and m["sender"] != "Unknown"})
        convo = "\n".join(f"{m['sender']}: {m['content'][:300]}" for m in ch)
        prompt = (
            f"Group: {g}\nKnown participants: {', '.join(senders[:60])}\n\n"
            f"Messages (chronological, 'Sender: text'):\n{convo}\n\n"
            "Identify EVERY distinct discussion topic/thread above. For each topic return:\n"
            '- "topic": specific concrete title (<=9 words), not "general chat"\n'
            '- "summary": 1-3 FACTUAL sentences: what was said/asked/decided/shared, with specifics\n'
            f'- "category": one of {DISC_CATS}\n'
            '- "participants": names (only from Known participants) who took part in THIS topic\n'
            '- "links": URLs that belong to this topic\n'
            'Skip pure greetings/thanks/birthday wishes. Return JSON {"topics":[...]}.'
        )
        try:
            res = o.chat_json(prompt, system=DISC_SYS)
            for t in (res.get("topics", []) if isinstance(res, dict) else []):
                if not isinstance(t, dict):
                    continue
                topic = _as_str(t.get("topic"))
                if not topic or topic.lower() in ("general chat", "greetings", "n/a"):
                    continue
                parts = [x for x in _as_list(t.get("participants")) if x in senders]
                if not parts:
                    parts = sorted({m["sender"] for m in ch if m.get("sender") != "Unknown"})[:6]
                raw.append({
                    "topic": topic, "summary": _as_str(t.get("summary")),
                    "category": (_as_str(t.get("category")) or "Discussion"),
                    "participants": sorted(set(parts)), "groups": [g],
                    "links": _as_list(t.get("links")), "message_count": len(ch),
                })
        except Exception as e:
            LOG.warning("discussion extract failed (%s): %s", g, str(e)[:80])
        progress.bar(k + 1, len(jobs), prefix="Extracting discussions")

    merged = _merge_topics(cfg, raw)
    LOG.info("Discussions: %d raw topics -> %d unique", len(raw), len(merged))
    return merged, low_activity


def _merge_topics(cfg: dict, topics: list[dict]) -> list[dict]:
    if not topics:
        return []
    o = Ollama(cfg)
    vecs = []
    for t in topics:
        try:
            vecs.append(np.array(o.embed(t["topic"][:200]), dtype=np.float32))
        except Exception:
            vecs.append(None)
    clusters = []  # {centroid, members:[idx]}
    for i, v in enumerate(vecs):
        if v is None:
            clusters.append({"centroid": None, "members": [i]}); continue
        vn = v / (np.linalg.norm(v) + 1e-9)
        best, bs = -1, 0.0
        for ci, cl in enumerate(clusters):
            if cl["centroid"] is None:
                continue
            sim = float(np.dot(vn, cl["centroid"]))
            if sim > bs:
                best, bs = ci, sim
        if bs >= 0.82:
            cl = clusters[best]; cl["members"].append(i)
            n = len(cl["members"])
            cl["centroid"] = (cl["centroid"] * (n - 1) + vn) / n
            cl["centroid"] /= (np.linalg.norm(cl["centroid"]) + 1e-9)
        else:
            clusters.append({"centroid": vn, "members": [i]})

    out = []
    for cl in clusters:
        members = [topics[i] for i in cl["members"]]
        members.sort(key=lambda t: len(t["summary"]), reverse=True)
        head = members[0]
        participants = sorted({p for t in members for p in t["participants"]})
        groups = sorted({g for t in members for g in t["groups"]})
        links = sorted({l for t in members for l in t["links"]})
        out.append({
            "topic": head["topic"], "summary": head["summary"],
            "category": head["category"], "participants": participants,
            "participant_count": len(participants), "groups": groups,
            "message_count": sum(t["message_count"] for t in members), "links": links,
        })
    out.sort(key=lambda d: (d["participant_count"], d["message_count"]), reverse=True)
    return out


# ── events with full logistics ───────────────────────────────────────────────
EVENT_SYS = ("You extract EVENTS (gatherings, courses, classes, webinars, retreats, meetings, "
             "deadlines, calls) with full logistics from WhatsApp messages and posters. Use ONLY "
             "stated information; leave a field empty if not stated. NEVER invent.")
EVENT_FIELDS = ["title", "date", "time", "venue", "location",
                "conference_link", "registration_link", "contact", "host"]


def extract_events(cfg: dict, unique: list[dict], today: dt.date) -> list[dict]:
    o = Ollama(cfg)
    domains = cfg.get("extraction", {}).get("event_link_domains", [])
    by_group = collections.defaultdict(list)
    for m in unique:
        by_group[m["group"]].append(m)
    jobs = []
    for g, gm in by_group.items():
        gm.sort(key=lambda m: m["timestamp"] or "")
        for ch in _chunks(gm, 5000, 45):
            jobs.append((g, ch))

    raw = []
    for k, (g, ch) in enumerate(jobs):
        convo = "\n".join(f"{m['sender']}: {m['content'][:400]}" for m in ch)
        urls = URL_RE.findall(convo)
        prompt = (
            "Extract EVERY event from these messages. For each event return these keys "
            '(use "" if not stated): "title", "date" (as written), "time", "venue" (hall/place '
            'name), "location" (address/city/area), "conference_link" (zoom/meet/teams/webex URL), '
            '"registration_link", "contact" (name and/or phone to RSVP), "host" (organiser/teacher/'
            'speaker). Include online and in-person events, courses, and deadlines.\n\n'
            f"Messages:\n{convo}\n\nReturn JSON {{\"events\":[...]}}."
        )
        try:
            res = o.chat_json(prompt, system=EVENT_SYS)
            for e in (res.get("events", []) if isinstance(res, dict) else []):
                if not isinstance(e, dict):
                    continue
                rec = {f: _as_str(e.get(f)) for f in EVENT_FIELDS}
                if not rec["title"]:
                    continue
                # backfill links from URLs present in the chunk
                if not rec["conference_link"] or not rec["registration_link"]:
                    for u in urls:
                        ul = u.lower()
                        if not rec["conference_link"] and any(d in ul for d in
                                                              ("zoom", "meet.google", "teams", "webex")):
                            rec["conference_link"] = u
                        elif not rec["registration_link"] and any(d in ul for d in domains):
                            rec["registration_link"] = u
                rec["group"] = g
                raw.append(rec)
        except Exception as e:
            LOG.warning("event extract failed (%s): %s", g, str(e)[:80])
        progress.bar(k + 1, len(jobs), prefix="Extracting events    ")

    return _merge_events(raw, today)


def _ord(d: str) -> int:
    try:
        return int(d.replace("-", ""))
    except Exception:
        return 0


def _richness(e: dict) -> int:
    return sum(bool(e.get(f)) for f in EVENT_FIELDS) * 2 + len(e.get("title", ""))


def _merge_events(events: list[dict], today: dt.date) -> list[dict]:
    stop = {"the", "a", "an", "reminder", "join", "us", "for", "on", "at", "event",
            "with", "and", "of", "to", "this", "next", "upcoming", "please", "online"}
    merged = {}
    for e in events:
        date_str = e.get("date", "")
        when = None
        if date_str:
            for dayfirst in (True, False):
                try:
                    when = dtparse.parse(date_str, dayfirst=dayfirst,
                                         default=dt.datetime(today.year, 1, 1))
                    break
                except Exception:
                    continue
        e["status"] = "Undated" if not when else ("Past" if when.date() < today else "Upcoming")
        e["_sort"] = when.date().isoformat() if when else ""
        toks = [t for t in re.findall(r"[a-z0-9]+", e["title"].lower()) if t not in stop and len(t) > 2]
        key = (e["_sort"] or "?") + "|" + "_".join(toks[:2])
        cur = merged.get(key)
        if not cur or _richness(e) > _richness(cur):
            if cur:
                for f in EVENT_FIELDS:
                    e[f] = e[f] or cur[f]
            merged[key] = e
    out = list(merged.values())
    order = {"Upcoming": 0, "Undated": 1, "Past": 2}
    out.sort(key=lambda e: (order[e["status"]],
                            e["_sort"] if e["status"] == "Upcoming" else "",
                            -_ord(e["_sort"]) if e["status"] == "Past" else 0))
    LOG.info("Events: %d raw -> %d unique", len(events), len(out))
    return out


# ── pleasantries bucket ──────────────────────────────────────────────────────
def summarise_pleasantries(pleasantries: list[dict]) -> dict:
    by_type = collections.Counter()
    people = collections.Counter()
    for m in pleasantries:
        by_type[m.get("_pleasantry_type", "greeting")] += 1
        if m.get("sender") and m["sender"] != "Unknown":
            people[m["sender"]] += 1
    return {
        "count": len(pleasantries),
        "by_type": dict(by_type.most_common()),
        "top_people": people.most_common(15),
    }


# ── factual highlights digest ────────────────────────────────────────────────
def make_highlights(cfg: dict, discussions: list[dict], events: list[dict]) -> str:
    o = Ollama(cfg)
    disc = "\n".join(f"- {d['topic']} ({d['participant_count']} people): {d['summary']}"
                     for d in discussions[:12])
    ev = "\n".join(f"- {e['title']} ({e.get('date') or 'date TBD'}"
                   f"{', ' + e['venue'] if e.get('venue') else ''})" for e in events[:15])
    prompt = (
        "Write a FACTUAL digest (4-6 sentences) of the most important things that happened, "
        "based ONLY on the items below. Lead with concrete events, decisions and announcements. "
        "No greetings, no filler, no meta-commentary, no mention of being AI-generated.\n\n"
        f"Discussions:\n{disc or '(none)'}\n\nEvents:\n{ev or '(none)'}\n\nReturn plain prose only."
    )
    try:
        return o.chat(prompt, system="You write concise, factual community news digests.")
    except Exception as e:
        LOG.warning("highlights failed: %s", e)
        return ""


# ── stats ────────────────────────────────────────────────────────────────────
def compute_stats(unique: list[dict], pleasantries: list[dict], discussions, events,
                  start: dt.date, end: dt.date) -> dict:
    senders, groups, days = collections.Counter(), collections.Counter(), collections.Counter()
    links = images = 0
    for m in unique:
        if m.get("timestamp"):
            days[m["timestamp"][:10]] += 1
        senders[m.get("sender", "Unknown")] += 1
        groups[m["group"]] += 1
        links += len(URL_RE.findall(m.get("content", "")))
        images += 1 if m.get("has_image") else 0
    span = max(1, (end - start).days)
    trend = []
    d = start
    while d <= end:
        trend.append({"date": d.isoformat(), "count": days.get(d.isoformat(), 0)})
        d += dt.timedelta(days=1)
    return {
        "total_unique": len(unique),
        "active_participants": len([s for s in senders if s != "Unknown"]),
        "groups_count": len(groups),
        "discussions_count": len(discussions),
        "events_count": len(events),
        "pleasantries_count": len(pleasantries),
        "links_shared": links,
        "images_shared": images,
        "avg_per_day": round(len(unique) / span, 1),
        "per_group": groups.most_common(),
        "top_contributors": senders.most_common(12),
        "trend": trend,
        "busiest_day": max(trend, key=lambda t: t["count"]) if trend else None,
    }


# ── orchestration ────────────────────────────────────────────────────────────
def run(cfg: dict):
    start, end = date_window(cfg)
    today = end
    msgs = load_messages(cfg)
    if not msgs:
        LOG.error("No messages found. Scrape first.")
        return None
    kept, pleasantries = clean(cfg, msgs)
    dd = dedupe(cfg, kept)
    unique = dd["unique"]
    discussions, low_activity = extract_discussions(cfg, unique)
    events = extract_events(cfg, unique, today)
    stats = compute_stats(unique, pleasantries, discussions, events, start, end)
    highlights = make_highlights(cfg, discussions, events)

    ex = cfg.get("extraction", {}) or {}
    analysis = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": dt.datetime.now().isoformat(),
        "criteria": {"capture": ex.get("capture", []),
                     "pleasantries_last": ex.get("pleasantries_last", True),
                     "min_messages_per_group": ex.get("min_messages_per_group", 3)},
        "groups_covered": [g for g, _ in stats["per_group"]],
        "dedup": dd["stats"],
        "stats": stats,
        "highlights": highlights,
        "discussions": discussions,
        "events": events,
        "pleasantries": summarise_pleasantries(pleasantries),
        "low_activity_groups": low_activity,
    }
    write_json(p(cfg, "processed") / "analysis.json", analysis)
    LOG.info("Analysis written -> %s", p(cfg, 'processed') / 'analysis.json')
    return analysis


def main():
    run(load_config())


if __name__ == "__main__":
    main()
