"""Configuration loader."""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "profile.yaml"


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load the YAML config file."""
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_filter_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("filters", {})


def get_stack_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("stack", {})


def get_scoring_weights(config: dict[str, Any]) -> dict[str, float]:
    return config.get("scoring", {})


def get_resume_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("resume", {})
