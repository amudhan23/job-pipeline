"""Web-first discovery: find job boards from the web, not from a hardcoded list."""

from .registry import BoardRegistry
from .feeds import SimplifyFeed, normalize_url

__all__ = ["BoardRegistry", "SimplifyFeed", "normalize_url"]
