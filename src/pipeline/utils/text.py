"""Text normalization and matching utilities."""

from __future__ import annotations

import re


def normalize_company_name(name: str) -> str:
    """Normalize company name for matching across databases.

    Strips suffixes like Inc, LLC, Corp, Ltd and normalizes whitespace/case.
    """
    name = name.strip().lower()
    # Remove common suffixes
    suffixes = [
        r",?\s+(inc\.?|llc\.?|corp\.?|corporation|ltd\.?|limited|co\.?|company|l\.?p\.?|plc)\s*$",
        r"^(.*?),\s*(inc\.?|llc\.?|corp\.?|corporation|ltd\.?|limited)\s*$",
        r"\s*\(.*?\)",  # Remove parenthetical
    ]
    for suffix in suffixes:
        name = re.sub(suffix, "", name, flags=re.IGNORECASE)
    # Normalize whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_experience_years(text: str) -> tuple[int, int] | None:
    """Extract experience range from job description text.

    Returns (min_years, max_years) or None if not found.
    """
    patterns = [
        r"(\d+)\s*[-–]\s*(\d+)\+?\s*years?",          # "3-5 years"
        r"(\d+)\+\s*years?",                            # "3+ years"
        r"at\s+least\s+(\d+)\s+years?",                 # "at least 3 years"
        r"minimum\s+(?:of\s+)?(\d+)\s+years?",          # "minimum 3 years"
        r"(\d+)\s+years?\s+(?:of\s+)?(?:experience|exp)", # "3 years experience"
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                return int(groups[0]), int(groups[1])
            return int(groups[0]), int(groups[0])
    return None


def matches_keywords(text: str, keywords: list[str], exclude: list[str] | None = None) -> bool:
    """Check if text matches any keyword and doesn't match exclusions."""
    text_lower = text.lower()
    if exclude:
        for exc in exclude:
            if exc.lower() in text_lower:
                return False
    return any(kw.lower() in text_lower for kw in keywords)


def extract_location(text: str) -> str:
    """Best-effort location extraction from a location string."""
    # Common patterns in ATS location fields
    text = text.strip()
    # Remove "Remote - " prefix but keep the city if present
    text = re.sub(r"^Remote\s*[-–]\s*", "", text, flags=re.IGNORECASE)
    # Remove country suffix
    text = re.sub(r",\s*United States.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r",\s*US$", "", text, flags=re.IGNORECASE)
    return text.strip()
