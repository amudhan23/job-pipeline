"""Job fit scorer — scores jobs 0-100 against the applicant profile."""

from __future__ import annotations

import re
import logging
from typing import Any

from pipeline.models import Job, SponsorshipLikelihood
from pipeline.utils.text import extract_experience_years

logger = logging.getLogger(__name__)


class FitScorer:
    """Score jobs against the applicant's profile and preferences."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.stack = config.get("stack", {})
        self.weights = config.get("scoring", {})
        self.filters = config.get("filters", {})

        # Build lookup sets for fast matching
        self.primary_langs = {s.lower() for s in self.stack.get("languages", {}).get("primary", [])}
        self.secondary_langs = {s.lower() for s in self.stack.get("languages", {}).get("secondary", [])}
        self.frameworks = {s.lower() for s in self.stack.get("frameworks", [])}
        self.databases = {s.lower() for s in self.stack.get("databases", [])}
        self.cloud = {s.lower() for s in self.stack.get("cloud", [])}
        self.tools = {s.lower() for s in self.stack.get("tools", [])}

        self.all_skills = (
            self.primary_langs | self.secondary_langs |
            self.frameworks | self.databases |
            self.cloud | self.tools
        )

    def score(self, job: Job) -> float:
        """Score a job 0-100. Higher = better fit."""
        breakdown: dict[str, float] = {}

        # Stack match (0-100)
        breakdown["stack_match"] = self._score_stack(job.description)

        # Experience alignment (0-100)
        breakdown["experience_alignment"] = self._score_experience(job.description, job.title)

        # Sponsorship likelihood (0-100)
        breakdown["sponsorship_likelihood"] = self._score_sponsorship(job.sponsorship)

        # Location match (0-100)
        breakdown["location_match"] = self._score_location(job.location)

        # Role type match (0-100)
        breakdown["role_type_match"] = self._score_role_type(job.title, job.department)

        # Weighted composite
        total = 0.0
        for key, score in breakdown.items():
            weight = self.weights.get(key, 0.2)
            total += score * weight

        job.fit_score = round(total, 1)
        job.fit_breakdown = breakdown
        return job.fit_score

    def score_jobs(self, jobs: list[Job]) -> list[Job]:
        """Score and sort a list of jobs by fit."""
        for job in jobs:
            self.score(job)
        return sorted(jobs, key=lambda j: j.fit_score, reverse=True)

    def _score_stack(self, description: str) -> float:
        """Score how well the job's required stack matches ours."""
        if not description:
            return 50.0  # Neutral if no description

        desc_lower = description.lower()
        found_primary = sum(1 for s in self.primary_langs if s in desc_lower)
        found_secondary = sum(1 for s in self.secondary_langs if s in desc_lower)
        found_frameworks = sum(1 for s in self.frameworks if s in desc_lower)
        found_databases = sum(1 for s in self.databases if s in desc_lower)
        found_cloud = sum(1 for s in self.cloud if s in desc_lower)

        # Weighted: primary languages matter most
        score = 0.0
        if self.primary_langs:
            score += 40 * (found_primary / len(self.primary_langs))
        if self.secondary_langs:
            score += 10 * (found_secondary / len(self.secondary_langs))
        if self.frameworks:
            score += 20 * (found_frameworks / len(self.frameworks))
        if self.databases:
            score += 15 * (found_databases / len(self.databases))
        if self.cloud:
            score += 15 * (found_cloud / len(self.cloud))

        return min(100.0, score)

    def _score_experience(self, description: str, title: str) -> float:
        """Score experience level alignment. Penalize if they want 5+ years."""
        exp = extract_experience_years(description)
        if not exp:
            # Check title for level signals
            title_lower = title.lower()
            if any(w in title_lower for w in ["senior", "staff", "principal", "lead"]):
                return 30.0
            if any(w in title_lower for w in ["junior", "entry", "new grad", "associate"]):
                return 90.0
            return 70.0  # No signal = neutral-positive

        min_years, max_years = exp
        our_years = self.filters.get("max_experience", 4)

        if min_years > our_years + 1:
            return 20.0  # Way over our level
        if min_years > our_years:
            return 50.0  # Stretch but possible
        if max_years <= our_years + 2:
            return 95.0  # Sweet spot
        return 70.0  # Wide range, we're on the low end

    def _score_sponsorship(self, likelihood: SponsorshipLikelihood) -> float:
        """Score based on sponsorship likelihood."""
        return {
            SponsorshipLikelihood.HIGH: 100.0,
            SponsorshipLikelihood.MEDIUM: 70.0,
            SponsorshipLikelihood.UNKNOWN: 50.0,
            SponsorshipLikelihood.LOW: 15.0,
        }[likelihood]

    def _score_location(self, location: str) -> float:
        """Score location match."""
        loc_lower = location.lower()
        target_locations = self.filters.get("locations", [])

        if "remote" in loc_lower:
            return 95.0

        for target in target_locations:
            if target.lower() in loc_lower:
                return 100.0

        # Partial match (e.g., "California" when we want "San Francisco")
        if any(region in loc_lower for region in ["new york", "california", "bay area"]):
            return 70.0

        return 30.0  # Wrong location

    def _score_role_type(self, title: str, department: str) -> float:
        """Score role type preference. Backend/systems/infra > frontend/mobile/ML."""
        text = f"{title} {department}".lower()

        preferred = ["backend", "infrastructure", "systems", "platform", "security", "distributed"]
        okay = ["software engineer", "full stack", "fullstack", "swe"]
        less_preferred = ["frontend", "mobile", "ios", "android", "ml", "machine learning", "data science"]

        if any(p in text for p in preferred):
            return 100.0
        if any(p in text for p in less_preferred):
            return 40.0
        if any(p in text for p in okay):
            return 80.0
        return 60.0  # Generic
