"""Extract text from every scraped image.

Strategy: run Tesseract first (fast, offline). If it yields too little text, the
image is likely a stylised poster, so escalate to the local Qwen vision model for
a clean transcription. Results are cached so re-runs are cheap.
"""
from __future__ import annotations

from pathlib import Path

import pytesseract
from PIL import Image

from common import LOG, ROOT, Ollama, load_config, p, read_json, write_json

VISION_PROMPT = (
    "You are reading an image shared in a WhatsApp group (often an event flyer/poster). "
    "Transcribe ALL text you can see, preserving names, dates, times, venues, phone numbers "
    "and links exactly. Then on a final line output 'EVENT: yes' if it advertises an event "
    "(with a date/time/venue), otherwise 'EVENT: no'. Do not add commentary."
)


def _load_image(path: Path):
    # pillow-heif registers HEIC support on import if available
    try:
        import pillow_heif  # noqa: F401
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    return Image.open(path).convert("RGB")


def run(cfg: dict) -> dict:
    pytesseract.pytesseract.tesseract_cmd = cfg["ocr"]["tesseract_cmd"]
    media_dir = p(cfg, "media")
    cache_path = p(cfg, "processed") / "image_text.json"
    cache = read_json(cache_path, default={}) or {}
    o = Ollama(cfg)
    min_chars = cfg["ocr"]["min_chars_for_confidence"]

    images = sorted([q for q in media_dir.iterdir()
                     if q.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic"}])
    LOG.info("OCR over %d image(s)...", len(images))

    for img_path in images:
        rel = str((Path(cfg["paths"]["media"]) / img_path.name).as_posix())
        if rel in cache:
            continue
        ocr_text, vision_text, is_event = "", "", False
        try:
            ocr_text = pytesseract.image_to_string(_load_image(img_path)).strip()
        except Exception as e:
            LOG.warning("  tesseract failed on %s: %s", img_path.name, e)

        if len(ocr_text) < min_chars:
            try:
                vis = o.vision(VISION_PROMPT, img_path)
                vision_text = vis
                is_event = "event: yes" in vis.lower()
            except Exception as e:
                LOG.warning("  vision failed on %s: %s", img_path.name, e)

        final = vision_text or ocr_text
        if not is_event:
            low = final.lower()
            is_event = any(w in low for w in ("rsvp", "register", "join us", "workshop",
                                              "seminar", "webinar", "venue", "p.m", "pm,", "am,"))
        cache[rel] = {
            "ocr_text": ocr_text,
            "vision_text": vision_text,
            "final_text": final.strip(),
            "is_event_poster": is_event,
            "source": "vision" if vision_text else "tesseract",
        }
        LOG.info("  %-28s %4d chars via %s%s", img_path.name, len(final),
                 cache[rel]["source"], "  [EVENT]" if is_event else "")
        write_json(cache_path, cache)  # incremental save

    write_json(cache_path, cache)
    LOG.info("Image text extraction complete -> %s", cache_path)
    return cache


if __name__ == "__main__":
    run(load_config())
