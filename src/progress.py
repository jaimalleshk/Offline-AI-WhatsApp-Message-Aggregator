"""Tiny, dependency-free console progress helpers (stage headers + a bar)."""
from __future__ import annotations

import sys


def _w(s: str) -> None:
    try:
        sys.stdout.write(s)
        sys.stdout.flush()
    except Exception:
        pass


def stage(i: int, n: int, name: str) -> None:
    """Print a numbered pipeline-stage header, e.g.  [2/4] Reading images…"""
    _w(f"\n[{i}/{n}] {name}\n")


def bar(current: int, total: int, prefix: str = "", width: int = 30) -> None:
    """Redraw an in-place progress bar. Call repeatedly with rising `current`.

    Uses plain ASCII so it renders in any console. Prints a newline when full.
    Do NOT interleave other prints between bar updates on the same line.
    """
    total = max(1, total)
    current = max(0, min(current, total))
    frac = current / total
    filled = int(round(width * frac))
    line = "#" * filled + "-" * (width - filled)
    _w(f"\r    {prefix} [{line}] {current}/{total} ({int(frac * 100)}%)   ")
    if current >= total:
        _w("\n")
