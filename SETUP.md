# Setup Guide

**WhatsApp Community Intelligence** — a fully offline tool that reads your WhatsApp
groups, de-duplicates across them, reads event posters (OCR + local vision), and
produces a professional categorised report (sentiment, discussion-health,
participation, events) as **HTML + PDF**. All AI runs **locally** on your machine
via Ollama — no cloud, no external API, no data leaves the device.

This guide takes a brand-new machine to a working install.

---

## 1. Prerequisites

### Hardware
| | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16 GB+ |
| GPU (VRAM) | none (CPU works, slow) | 6 GB+ NVIDIA/Apple Silicon |
| Disk | ~10 GB free (models) | 15 GB+ |

> On a 6 GB GPU, use the **3B** analysis model (`qwen2.5:3b-instruct`) rather than 7B —
> the 7B may spill to CPU and run slowly.

### Software (install these first)
| Tool | Version | Where |
|---|---|---|
| **Python** | 3.11+ | https://www.python.org/downloads/ (tick *“Add to PATH”*) |
| **Git** | any | https://git-scm.com/downloads |
| **Ollama** | latest | https://ollama.com/download |
| **Tesseract OCR** | 5.x | Windows: https://github.com/UB-Mannheim/tesseract/wiki · macOS: `brew install tesseract` · Linux: `apt install tesseract-ocr` |

### Account
A normal **WhatsApp account** on your phone (to link this tool as a *linked device*
via QR — one time).

---

## 2. Install

```bash
# 1. Clone
git clone https://github.com/<your-user>/<your-repo>.git
cd <your-repo>

# 2. Python dependencies
pip install -r requirements.txt

# 3. Headless browser used for scraping + PDF rendering
python -m playwright install chromium

# 4. Pull the local models (one-time download)
ollama pull qwen2.5:7b-instruct      # or qwen2.5:3b-instruct on small GPUs
ollama pull qwen2.5vl:7b             # vision model for event posters (optional)
ollama pull nomic-embed-text         # cross-group de-duplication
```

Make sure **Ollama is running** (`ollama serve`, or the desktop app open) before use.

---

## 3. Configure

Edit **`config.yaml`**:

```yaml
models:
  analysis: "qwen2.5:7b-instruct"    # use 3b-instruct on a 6 GB GPU
  vision:   "qwen2.5vl:7b"
  embed:    "nomic-embed-text:latest"

ocr:
  tesseract_cmd: "C:/Program Files/Tesseract-OCR/tesseract.exe"   # path to YOUR install

report:
  title: "WhatsApp Community Intelligence Report"
  organisation: "Your Org"           # header badge = initials

scrape:
  gentle_mode: true                  # human-like pacing to reduce ban risk
```

> **Groups are NOT configured here.** You name the groups/chats in plain English each
> time you run the agent (see below). `keywords:` stays empty by design.

---

## 4. Run

```bash
cd src
python agent.py
```

This opens a **menu-driven console** — every option is visible, nothing hidden:

```
  1) Generate report   (Full: scrape → analyse → report)
  2) Generate report   (Skip scraping — reuse downloaded data)
  3) Generate report   (Report only — instant rebuild, no AI)
  4) Settings          (AI model, gentle mode)
  5) Open report folder
  6) Help / About
  0) Quit
```

**Generate a report:** pick a mode → choose one of your **last 3 requests** (or write a
new one) → a **review screen** lets you change the time window, groups/chats, sections,
outputs, mode, focus and title → **RUN**. A **live progress display** shows each stage
(`[2/4] Analysing…`) and a bar (`Classifying messages [####----] 14/28`).

Describe a request in plain English, e.g.:

> *Last 3 weeks, the volunteer and events groups. Categorise, read event posters,
> assess discussion health, ignore cross-group duplicates. PDF and HTML.*

**First run only:** a Chromium window opens — scan the QR code
(*WhatsApp → Settings → Linked Devices → Link a device*). The login is saved in
`profile/`, so later runs are unattended.

### The three run modes (menu options 1–3)
| Mode | What it does | Re-scrape? | Re-analyse? | Speed |
|---|---|---|---|---|
| **Full** | scrape WhatsApp → analyse → report | yes | yes | slow |
| **Skip scraping** | re-analyse data already downloaded | no | yes | minutes |
| **Report only** | rebuild report from processed data | no | no | instant |

> **Settings (option 4)** lets you switch the **analysis model** (pick any installed
> Ollama model, e.g. `qwen2.5:3b-instruct` or `gemma4:12b-it-qat`) and toggle
> **gentle mode** — both saved to `config.yaml`.

For scripting/automation, the same actions are available as flags
(`--intent "…" --yes`, `--skip-scrape`, `--report-only`).

Outputs land in **`output/`**: `report.html`, `report.pdf`, and optionally
`events.csv`, `summary.csv`, `report_data.json`.

### Preview without WhatsApp
```bash
python tools/make_sample_report.py     # writes samples/sample_report.html + .pdf (fake data)
```

---

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `Ollama is not running` | Start the Ollama app or `ollama serve`. |
| `tesseract is not installed` | Set the correct `ocr.tesseract_cmd` path in `config.yaml`. |
| Scrape finds **0 chats** | Run `python scrape_whatsapp.py --inspect` and check the diagnostic; WhatsApp Web DOM may differ. |
| A group shows **0 messages** | It may be a quiet/1-on-1 chat, or open it manually once so history loads, then re-run. |
| Report too slow | Use `qwen2.5:3b-instruct`; reduce groups; the scroll-back phase is the slow part. |
| Unicode errors in console | Already handled (UTF-8); ensure you're on the provided scripts. |

---

## 6. Important: legality, privacy & account safety

- Reading WhatsApp via automation is **against WhatsApp's Terms of Service**, even with
  your own account. The main practical risk is a **temporary account/number ban**. This
  tool is **read-only** (never sends/joins/posts) and `gentle_mode` paces it to look
  human — but use it sparingly and at your own risk.
- Reports contain **other people's** messages, names, and photos. Keep them **private and
  local**. Do **not** publish or share reports that identify individuals. For anything
  beyond personal use — or if members are in the EU/CA — get appropriate legal advice
  (GDPR/CCPA, consent).
- Everything runs **on-device**; no data is sent to any cloud or AI provider.

---

## 7. What is (and isn't) in this repo

Committed: source (`src/`), templates, tools, a **synthetic sample report**
(`samples/`), config, docs.

Never committed (see `.gitignore`): your `profile/` (login), `data/` (scraped
messages/media/analysis), and `output/` (real reports).
