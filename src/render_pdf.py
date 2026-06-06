"""Render the HTML report to a print-faithful PDF using Playwright/Chromium."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from common import LOG, load_config, p

HEADER = '<div></div>'


def _footer(cfg: dict) -> str:
    title = (cfg.get("report", {}) or {}).get("title", "WhatsApp Community Intelligence Report")
    title = title.replace("<", "").replace(">", "")
    return (
        '<div style="width:100%;font-size:7px;color:#9aa7b2;padding:0 14mm;'
        'font-family:Segoe UI,Arial,sans-serif;display:flex;justify-content:space-between;">'
        f'<span>{title} · Confidential</span>'
        '<span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
    )


def run(cfg: dict) -> Path:
    html = p(cfg, "output") / "report.html"
    pdf = p(cfg, "output") / "report.pdf"
    if not html.exists():
        raise SystemExit("report.html missing — run report.py first.")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.goto(html.resolve().as_uri(), wait_until="networkidle")
        page.pdf(
            path=str(pdf),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=HEADER,
            footer_template=_footer(cfg),
            margin={"top": "6mm", "bottom": "11mm", "left": "0mm", "right": "0mm"},
        )
        b.close()
    LOG.info("PDF report -> %s", pdf)
    return pdf


if __name__ == "__main__":
    run(load_config())
