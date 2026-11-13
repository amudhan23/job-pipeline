"""Calling the Claude API with your own key, inside a monthly spending cap.

Everything private lives in data/private/ (gitignored, readable only by you):
  anthropic_key     your API key (or set ANTHROPIC_API_KEY instead)
  ai_settings.json  model + monthly budget
  ai_usage.jsonl    one line per call: tokens and what it cost

Before each call the month's spend so far is checked against the budget, so
a bug or a big batch can't run up your credits.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

KEY_FILE = "anthropic_key"
SETTINGS_FILE = "ai_settings.json"
USAGE_FILE = "ai_usage.jsonl"

MODELS = {
    "sonnet": "claude-sonnet-5-5",   # default: good writing, modest price
    "haiku": "claude-haiku-5-5",     # cheapest
    "opus": "claude-opus-5-5",       # strongest
}
# USD per million tokens: (input, output, cache read, cache write).
# Haiku 5.5's rates apply to prompts up to 100K tokens — ours are a few
# thousand. Check https://platform.claude.com/docs/en/about-claude/pricing
# if Anthropic changes them.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
DEFAULTS = {"model": "sonnet", "monthly_budget": 5.0, "enabled": True}


class AIUnavailable(Exception):
    """No key, no SDK, AI switched off, or the budget is spent."""


def _pdir(private_dir: Path | None) -> Path:
    from pipeline import applicant
    return private_dir or applicant.PRIVATE_DIR


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o600)


# ── Key ────────────────────────────────────────────────────────────────

def get_key(private_dir: Path | None = None) -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    path = _pdir(private_dir) / KEY_FILE
    return path.read_text().strip() or None if path.exists() else None


def save_key(key: str, private_dir: Path | None = None) -> None:
    key = key.strip()
    if not key.startswith("sk-ant-") or len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("That doesn't look like an Anthropic API key (they start with sk-ant-)")
    _write_private(_pdir(private_dir) / KEY_FILE, key)


def forget_key(private_dir: Path | None = None) -> None:
    path = _pdir(private_dir) / KEY_FILE
    if path.exists():
        path.unlink()


def masked_key(private_dir: Path | None = None) -> str:
    key = get_key(private_dir)
    return f"{key[:7]}…{key[-4:]}" if key else ""


# ── Settings & spend ───────────────────────────────────────────────────

def load_settings(private_dir: Path | None = None) -> dict[str, Any]:
    path = _pdir(private_dir) / SETTINGS_FILE
    data = dict(DEFAULTS)
    if path.exists():
        data.update(json.loads(path.read_text()))
    return data


def save_settings(values: dict[str, Any], private_dir: Path | None = None) -> dict[str, Any]:
    data = load_settings(private_dir)
    if "model" in values:
        if values["model"] not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        data["model"] = values["model"]
    if "monthly_budget" in values:
        budget = float(values["monthly_budget"])
        if not 0 <= budget <= 1000:
            raise ValueError("monthly budget must be between $0 and $1000")
        data["monthly_budget"] = round(budget, 2)
    if "enabled" in values:
        data["enabled"] = bool(values["enabled"])
    _write_private(_pdir(private_dir) / SETTINGS_FILE, json.dumps(data, indent=2))
    return data


def cost_of(model: str, usage: dict[str, int]) -> float:
    inp, out, cread, cwrite = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (usage.get("input_tokens", 0) * inp + usage.get("output_tokens", 0) * out
            + usage.get("cache_read_input_tokens", 0) * cread
            + usage.get("cache_creation_input_tokens", 0) * cwrite) / 1_000_000


def usage_log(private_dir: Path | None = None) -> list[dict[str, Any]]:
    path = _pdir(private_dir) / USAGE_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def spend_summary(private_dir: Path | None = None) -> dict[str, Any]:
    log = usage_log(private_dir)
    month = datetime.now().strftime("%Y-%m")
    this_month = [e for e in log if e.get("at", "").startswith(month)]
    settings = load_settings(private_dir)
    spent = sum(e.get("cost", 0) for e in this_month)
    return {
        "month_spent": round(spent, 4),
        "month_calls": len(this_month),
        "total_spent": round(sum(e.get("cost", 0) for e in log), 4),
        "monthly_budget": settings["monthly_budget"],
        "remaining": round(max(0.0, settings["monthly_budget"] - spent), 4),
        "recent": log[-10:][::-1],
    }


def _record(private_dir: Path | None, entry: dict[str, Any]) -> None:
    path = _pdir(private_dir) / USAGE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    os.chmod(path, 0o600)


# ── Calling the API ────────────────────────────────────────────────────

@dataclass
class Reply:
    text: str
    model: str
    cost: float


def status(private_dir: Path | None = None) -> tuple[bool, str]:
    """(ready, why not) — without spending anything."""
    settings = load_settings(private_dir)
    if not settings.get("enabled", True):
        return False, "AI is switched off on My details"
    if not get_key(private_dir):
        return False, "no Anthropic API key saved (dashboard → My details → AI, or `jobpipe ai key`)"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "the anthropic package isn't installed: pip install -e '.[ai]'"
    if spend_summary(private_dir)["remaining"] <= 0:
        return False, "this month's AI budget is used up (raise it on My details)"
    return True, ""


def complete(system: list[dict] | str, prompt: str, purpose: str, max_tokens: int = 600,
             private_dir: Path | None = None, client: Any = None) -> Reply:
    """One Messages API call, budget-checked and logged."""
    ready, why = status(private_dir)
    if not ready:
        raise AIUnavailable(why)
    settings = load_settings(private_dir)
    model = MODELS[settings["model"]]
    # Worst case for this call must still fit the budget
    worst = cost_of(model, {"input_tokens": (len(str(system)) + len(prompt)) // 3,
                            "output_tokens": max_tokens})
    if worst > spend_summary(private_dir)["remaining"]:
        raise AIUnavailable("not enough of this month's AI budget left for this request")
    if client is None:
        import logging
        import anthropic
        for name in ("httpx", "httpx2", "anthropic"):   # no per-request log lines in the terminal
            logging.getLogger(name).setLevel(logging.WARNING)
        client = anthropic.Anthropic(api_key=get_key(private_dir))
    msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": prompt}])
    u = msg.usage
    usage = {k: int(getattr(u, k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    cost = cost_of(model, usage)
    _record(private_dir, {"at": datetime.now().isoformat(timespec="seconds"), "purpose": purpose,
                          "model": model, **usage, "cost": round(cost, 6)})
    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
    return Reply(text=text, model=model, cost=cost)
