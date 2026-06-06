"""End-to-end orchestrator.

Usage:
  python src/pipeline.py                 # scrape -> ocr -> analyze -> report -> pdf
  python src/pipeline.py --skip-scrape   # reuse data already in data/raw
  python src/pipeline.py --only report   # run a single stage (scrape|ocr|analyze|report|pdf)
"""
from __future__ import annotations

import argparse

import analyze
import ocr_images
import report as report_mod
import render_pdf
from common import LOG, load_config, wait_for_ollama


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-scrape", action="store_true")
    ap.add_argument("--only", choices=["scrape", "ocr", "analyze", "report", "pdf"])
    args = ap.parse_args()
    cfg = load_config()

    if not wait_for_ollama(cfg):
        LOG.error("Ollama is not responding at %s — start it with `ollama serve`.",
                  cfg["models"]["ollama_host"])
        return

    stages = ["scrape", "ocr", "analyze", "report", "pdf"]
    run = [args.only] if args.only else stages
    if args.skip_scrape and "scrape" in run:
        run.remove("scrape")

    if "scrape" in run:
        LOG.info("=== STAGE: scrape ===")
        import scrape_whatsapp
        scrape_whatsapp.main()
    if "ocr" in run:
        LOG.info("=== STAGE: ocr ===")
        ocr_images.run(cfg)
    if "analyze" in run:
        LOG.info("=== STAGE: analyze ===")
        analyze.main()
    if "report" in run:
        LOG.info("=== STAGE: report ===")
        report_mod.run(cfg)
    if "pdf" in run:
        LOG.info("=== STAGE: pdf ===")
        render_pdf.run(cfg)
    LOG.info("Pipeline complete.")


if __name__ == "__main__":
    main()
