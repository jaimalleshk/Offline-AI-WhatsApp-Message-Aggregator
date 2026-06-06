"""Shared utilities: config, paths, logging, and a thin local-Ollama client.

Nothing here touches the network beyond the local Ollama server, keeping the
whole pipeline offline.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent


# ── config / paths ──────────────────────────────────────────────────────────
def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_root"] = str(ROOT)
    return cfg


def p(cfg: dict, key: str) -> Path:
    """Resolve a configured path relative to the project root and ensure it exists."""
    d = ROOT / cfg["paths"][key]
    d.mkdir(parents=True, exist_ok=True)
    return d


def date_window(cfg: dict) -> tuple[dt.date, dt.date]:
    dr = cfg["date_range"]
    end_cfg = dr.get("end")
    end = dt.date.fromisoformat(end_cfg) if end_cfg else dt.date.today()
    if dr.get("days_back"):                       # "past X days" (agent-driven)
        start = end - dt.timedelta(days=int(dr["days_back"]))
    else:
        start = end - dt.timedelta(weeks=int(dr.get("weeks_back", 3)))
    return start, end


def slug(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (s[:maxlen] or "untitled").lower()


# ── logging ─────────────────────────────────────────────────────────────────
def get_logger(name: str = "wa") -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    for _s in (sys.stdout, sys.stderr):       # UTF-8 on Windows consoles
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    return log


LOG = get_logger()


# ── Ollama client (chat / json / vision / embeddings) ───────────────────────
@dataclass
class Ollama:
    cfg: dict
    host: str = field(default="")

    def __post_init__(self):
        self.host = self.cfg["models"]["ollama_host"].rstrip("/")

    def _post(self, endpoint: str, payload: dict, timeout: int = 600) -> dict:
        r = requests.post(f"{self.host}{endpoint}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def chat(self, prompt: str, system: str | None = None, model: str | None = None,
             temperature: float = 0.2) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        out = self._post("/api/chat", {
            "model": model or self.cfg["models"]["analysis"],
            "messages": msgs,
            "stream": False,
            "options": {"temperature": temperature},
        })
        return out["message"]["content"].strip()

    def chat_json(self, prompt: str, system: str | None = None, model: str | None = None,
                  temperature: float = 0.1, retries: int = 2) -> Any:
        """Chat constrained to JSON output, parsed and returned."""
        sys_msg = (system or "") + "\nRespond with ONLY valid JSON. No prose, no markdown fences."
        last = ""
        for attempt in range(retries + 1):
            msgs = [{"role": "system", "content": sys_msg},
                    {"role": "user", "content": prompt}]
            out = self._post("/api/chat", {
                "model": model or self.cfg["models"]["analysis"],
                "messages": msgs,
                "stream": False,
                "format": "json",
                "options": {"temperature": temperature},
            })
            last = out["message"]["content"].strip()
            parsed = _extract_json(last)
            if parsed is not None:
                return parsed
            LOG.warning("JSON parse failed (attempt %d), retrying...", attempt + 1)
        raise ValueError(f"Could not parse JSON from model. Last output:\n{last[:500]}")

    def vision(self, prompt: str, image_path: str | Path, model: str | None = None) -> str:
        b = Path(image_path).read_bytes()
        out = self._post("/api/chat", {
            "model": model or self.cfg["models"]["vision"],
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(b).decode()],
            }],
            "stream": False,
            "options": {"temperature": 0.1},
        })
        return out["message"]["content"].strip()

    def embed(self, text: str, model: str | None = None) -> list[float]:
        out = self._post("/api/embeddings", {
            "model": model or self.cfg["models"]["embed"],
            "prompt": text,
        })
        return out["embedding"]

    def available_models(self) -> list[str]:
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=30)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []


def _extract_json(text: str) -> Any:
    text = text.strip()
    # strip ``` fences if present
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # last resort: grab the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                continue
    return None


def read_json(path: str | Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str | Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def wait_for_ollama(cfg: dict, timeout: int = 30) -> bool:
    o = Ollama(cfg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if o.available_models():
            return True
        time.sleep(1)
    return False
