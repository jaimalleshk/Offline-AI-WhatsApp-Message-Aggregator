# WhatsApp News Aggregation — Community Intelligence Report

Fully **offline** pipeline that scrapes WhatsApp groups matching a set of keywords,
reads text **and images/event posters**, de-duplicates across groups, categorises,
scores sentiment & conversation health, and produces a compact **professional
HTML + PDF report**.

**No cloud AI is used.** All language/vision/embedding work runs locally via
[Ollama](https://ollama.com) (Qwen 2.5) + Tesseract OCR.

---

## What it produces

`output/report.html` and `output/report.pdf` — identical, print-faithful report led by:

- **Quantitative stats**: unique messages, participants, groups, msgs/day, images, links, questions
- **De-duplication**: duplicate copies suppressed + count of topics that appeared in >1 group
- **Trend**: daily activity bar chart (weekends highlighted)
- **Sentiment**: positive/neutral/negative split + net score
- **Discussion health**: positivity & argumentativeness, per group (Healthy → Argumentative)
- **Participation/engagement**: top contributors, msgs/participant, questions, media, links
- **Per-group digests**: summary, highlights, repeated-conversation detection
- **Events**: extracted from text + posters, **upcoming first, past listed last**
- Report criteria (start/end dates, keywords) shown in the header band

---

## One-time setup

```bash
pip install -r requirements.txt
python -m playwright install chromium

# Local models (via Ollama):
ollama pull qwen2.5:3b-instruct      # analysis (fits 6 GB GPU; ~3 s/call)
ollama pull nomic-embed-text         # cross-group de-duplication
ollama pull qwen2.5vl:3b             # OPTIONAL: vision fallback for posters
# Tesseract OCR must be installed; path is set in config.yaml
```

> **Hardware note.** On the 6 GB GTX 1660 Ti in this machine, `qwen2.5:7b` only
> partially fits (CPU-offloaded, ~7 min/call) and is impractical for batch work,
> so the default analysis model is `qwen2.5:3b`. If you free up GPU memory you can
> switch back to 7b in `config.yaml`.

---

## Configure

Edit `config.yaml`:
- `date_range.weeks_back` (default **3**) and optional fixed `end` date
- `keywords` — a group is included if its title contains any of these
- `models`, `dedup.similarity_threshold`, `scrape.*`, `report.*`

---

## Run — the AI agent (recommended)

A self-contained agent that runs **entirely on your local Qwen model** (zero cloud /
zero Claude usage). You describe what you want in plain English; the local model
turns it into a run-plan and drives everything.

```bash
python src/agent.py
```

It asks what report you want. Type something like:

> *Report for the last 10 days across my volunteer and events groups. Focus on upcoming
> events and any conflicts. I want the sentiment, group digests and events sections,
> as a PDF and a CSV of events.*

The agent prints the interpreted plan (period, groups, sections, outputs), asks you
to confirm, then produces the files in `output/`.

You can control:
- **Past X days** — "last 10 days", "past 2 weeks", "this month" → resolved to a date window
- **Which groups / chats** — by keyword ("the volunteer groups") or exact name ("my chat with Priya"); groups *and* one-to-one chats, read or unread alike
- **Which analyses** — trend, sentiment & health, categories, group volume, participation, group digests, events
- **Outputs** — `pdf`, `html`, `csv` (events + summary), `json`

Non-interactive / scripted:
```bash
python src/agent.py --intent "last 7 days, all groups, full analysis, pdf+csv" --yes
python src/agent.py --skip-scrape --intent "..."   # reuse data/raw (re-runs OCR+analysis)
python src/agent.py --report-only --intent "all sections, pdf html csv"  # rebuild report
                                                   # ONLY (no scrape, no analysis) — instant
```

**Three speeds of re-run:**
| Need | Use | Re-scrapes? | Re-analyses (Qwen)? |
|---|---|---|---|
| Fresh data | (default) | yes | yes |
| Same chats, re-process | `--skip-scrape` | no | yes |
| Just change report look/sections | `--report-only` (or say *"regenerate report"*) | no | no |

`--report-only` rebuilds HTML/PDF/CSV straight from `data/processed/analysis.json`, so
tweaking layout, sections, or output formats is instant and needs no model.

The **first** run opens a Chromium window — **scan the WhatsApp QR code once**.
Login is saved in `profile/`, so later runs are unattended.

### Or run the fixed pipeline directly
```bash
python src/pipeline.py                     # scrape -> ocr -> analyze -> report -> pdf
python src/pipeline.py --skip-scrape       # reuse data already in data/raw
python src/pipeline.py --only report       # re-render report only
python src/scrape_whatsapp.py --inspect    # dump live page HTML for selector tuning
python tools/make_sample.py                # generate sample data to preview the report
```

> **Tip:** to preview the whole thing without WhatsApp, run `python tools/make_sample.py`
> then `python src/agent.py --skip-scrape --yes --intent "full report, last 3 weeks"`.

---

## How it works

| Stage | File | Notes |
|-------|------|-------|
| Scrape | `src/scrape_whatsapp.py` | Persistent-profile Playwright. Discovers keyword-matching groups via search, scrolls ~3 weeks of history, captures text + images (incrementally, to survive the virtualised message list). |
| Image text | `src/ocr_images.py` | Tesseract first; escalates low-confidence/poster images to the local Qwen-VL model. Flags event posters. |
| Analyse | `src/analyze.py` | Embedding-based cross-group de-dup; per-message category/sentiment/event extraction; per-group summary + health; full statistics. |
| Report | `src/report.py` + `templates/report.html.j2` | Compact, professional HTML with CSS charts. |
| PDF | `src/render_pdf.py` | Chromium print-to-PDF with page numbers/footer. |

---

## Notes & limitations

- **WhatsApp Web DOM is obfuscated and changes over time.** Selectors are written
  defensively, but on the first live run you may need minor tuning — use
  `--inspect` to dump the page and adjust selectors in `scrape_whatsapp.py`.
- Scraping your own chats via WhatsApp Web is for personal/community use; be mindful
  of WhatsApp's Terms of Service.
- Everything stays on-device; no data is sent to any external service.
