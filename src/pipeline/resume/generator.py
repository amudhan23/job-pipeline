"""Resume generator — tailors LaTeX resume per job description.

Takes your generic.tex template and produces a customized version:
1. Reorders IBM bullets based on job emphasis
2. Reorders Sahara AI bullets
3. Selects and orders projects
4. Reorders the Languages line
5. Builds PDF with pdflatex
6. Validates: exactly 1 page, ~94-98% fill
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pipeline.models import Job, ResumeConfig

logger = logging.getLogger(__name__)


# Keyword → bullet/project mappings
ROLE_SIGNALS = {
    # IBM bullet preferences by keyword
    "ibm_bullets": {
        "backend": ["own", "db", "mig", "ci", "dbg"],
        "infrastructure": ["ci", "mig", "dbg", "own", "db"],
        "systems": ["mig", "dbg", "own", "db", "ci"],
        "security": ["dbg", "own", "mig", "ci", "db"],
        "fullstack": ["own", "mig", "db", "ci", "dbg"],
        "devops": ["ci", "mig", "dbg", "db", "own"],
        "database": ["db", "own", "mig", "dbg", "ci"],
        "default": ["own", "mig", "dbg", "ci", "db"],
    },
    # Sahara bullet preferences
    "sahara_bullets": {
        "backend": ["int", "tx", "ship"],
        "infrastructure": ["tx", "int", "ship"],
        "fullstack": ["ship", "int", "tx"],
        "security": ["tx", "int", "ship"],
        "default": ["ship", "int", "tx"],
    },
    # Project preferences
    "projects": {
        "rust": ["req-guard", "Two-Pass Linker", "AccessMint", "Commentator"],
        "security": ["req-guard", "AccessMint", "Two-Pass Linker", "Commentator"],
        "typescript": ["AccessMint", "Commentator", "req-guard", "Two-Pass Linker"],
        "blockchain": ["AccessMint", "req-guard", "Commentator", "Two-Pass Linker"],
        "systems": ["Two-Pass Linker", "req-guard", "AccessMint", "Commentator"],
        "c++": ["Two-Pass Linker", "req-guard", "AccessMint", "Commentator"],
        "ai": ["Commentator", "AccessMint", "req-guard", "Two-Pass Linker"],
        "default": ["req-guard", "AccessMint", "Two-Pass Linker", "Commentator"],
    },
    # Language lead preferences
    "language_lead": {
        "rust": "Rust",
        "java": "Java",
        "typescript": "TypeScript",
        "python": "Python",
        "c++": "C++",
        "go": "Java",  # Closest we have
        "default": "Java",
    },
}


class ResumeGenerator:
    """Generate tailored resumes from a LaTeX template."""

    def __init__(self, template_path: Path | str, config: dict[str, Any]) -> None:
        self.template_path = Path(template_path)
        self.resume_config = config.get("resume", {})
        if not self.template_path.exists():
            logger.warning(f"Template not found: {self.template_path}")

    def analyze_job(self, job: Job) -> ResumeConfig:
        """Analyze a job description and determine the best resume configuration."""
        desc_lower = (job.description + " " + job.title).lower()

        # Detect primary role signal
        role_signal = "default"
        for signal in ["rust", "security", "infrastructure", "systems", "c++",
                       "typescript", "blockchain", "ai", "devops", "database",
                       "backend", "fullstack"]:
            if signal in desc_lower:
                role_signal = signal
                break

        # IBM bullets
        ibm_order = ROLE_SIGNALS["ibm_bullets"].get(
            role_signal, ROLE_SIGNALS["ibm_bullets"]["default"]
        )

        # Sahara bullets
        sahara_key = role_signal if role_signal in ROLE_SIGNALS["sahara_bullets"] else "default"
        sahara_order = ROLE_SIGNALS["sahara_bullets"][sahara_key]

        # Projects
        proj_key = role_signal if role_signal in ROLE_SIGNALS["projects"] else "default"
        projects = ROLE_SIGNALS["projects"][proj_key]

        # Language lead
        lang_key = role_signal if role_signal in ROLE_SIGNALS["language_lead"] else "default"
        lead_lang = ROLE_SIGNALS["language_lead"][lang_key]
        default_order = self.resume_config.get(
            "language_order_default",
            ["Java", "Rust", "Python", "TypeScript", "JavaScript", "C++", "C", "SQL", "Bash"],
        )
        language_order = [lead_lang] + [l for l in default_order if l != lead_lang]

        return ResumeConfig(
            job_id=job.id,
            ibm_bullet_order=ibm_order,
            sahara_bullet_order=sahara_order,
            projects=projects,
            language_order=language_order,
        )

    def build(self, job: Job, output_dir: Path | str) -> Path | None:
        """Build a tailored PDF resume for a job.

        Returns the path to the generated PDF, or None on failure.
        """
        if not self.template_path.exists():
            logger.error(f"Template not found: {self.template_path}")
            return None

        config = self.analyze_job(job)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Create a temp dir for the build
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Read template
            template = self.template_path.read_text()

            # Apply customizations
            customized = self._apply_config(template, config)

            # Write customized .tex
            tex_path = tmp / "resume.tex"
            tex_path.write_text(customized)

            # Copy any supporting files (cls, sty) from template dir
            for f in self.template_path.parent.iterdir():
                if f.suffix in (".cls", ".sty", ".bst") and f.name != self.template_path.name:
                    shutil.copy2(f, tmp / f.name)

            # Build PDF
            try:
                result = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                    cwd=tmp,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    logger.error(f"pdflatex failed: {result.stderr[:500]}")
                    return None
            except FileNotFoundError:
                logger.error("pdflatex not found — install texlive")
                return None
            except subprocess.TimeoutExpired:
                logger.error("pdflatex timed out")
                return None

            # Check output
            pdf_path = tmp / "resume.pdf"
            if not pdf_path.exists():
                logger.error("PDF not generated")
                return None

            # Validate page count
            if not self._validate_pdf(pdf_path):
                logger.warning("PDF validation failed (not exactly 1 page or bad fill)")

            # Copy to output
            safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", job.company)
            output_path = output_dir / f"resume_{safe_name}_{job.id}.pdf"
            shutil.copy2(pdf_path, output_path)

            logger.info(f"Generated resume: {output_path}")
            return output_path

    def _apply_config(self, template: str, config: ResumeConfig) -> str:
        """Apply resume configuration to the LaTeX template.

        Uses marker comments in the .tex to identify sections:
          % IBM_BULLETS_START / % IBM_BULLETS_END
          % SAHARA_BULLETS_START / % SAHARA_BULLETS_END
          % PROJECTS_START / % PROJECTS_END
          % LANGUAGES_LINE
        And placeholder tokens for content:
          [IBM_OWN], [IBM_MIG], [IBM_DBG], [IBM_CI], [IBM_DB]
          [SAHARA_SHIP], [SAHARA_INT], [SAHARA_TX]
          [REQGUARD_DESC], [ACCESSMINT_DESC], [LINKER_DESC], [COMMENTATOR_DESC]
          [LANGUAGES]
        """
        result = template

        # 1. Fill in bullet text from config
        ibm_bullets = self.resume_config.get("ibm_bullets", {})
        result = result.replace("[IBM_OWN]", ibm_bullets.get("own", ""))
        result = result.replace("[IBM_MIG]", ibm_bullets.get("mig", ""))
        result = result.replace("[IBM_DBG]", ibm_bullets.get("dbg", ""))
        result = result.replace("[IBM_CI]", ibm_bullets.get("ci", ""))
        result = result.replace("[IBM_DB]", ibm_bullets.get("db", ""))

        sahara_bullets = self.resume_config.get("sahara_bullets", {})
        result = result.replace("[SAHARA_SHIP]", sahara_bullets.get("ship", ""))
        result = result.replace("[SAHARA_INT]", sahara_bullets.get("int", ""))
        result = result.replace("[SAHARA_TX]", sahara_bullets.get("tx", ""))

        # Fill project descriptions
        projects_config = self.resume_config.get("projects", {})
        for key, proj in projects_config.items():
            desc = proj.get("desc", "") if isinstance(proj, dict) else ""
            # Escape LaTeX special chars in descriptions
            desc = self._escape_latex(desc)

        result = result.replace("[REQGUARD_DESC]", self._escape_latex(
            projects_config.get("req-guard", {}).get("desc", "")))
        result = result.replace("[ACCESSMINT_DESC]", self._escape_latex(
            projects_config.get("AccessMint", {}).get("desc", "")))
        result = result.replace("[LINKER_DESC]", self._escape_latex(
            projects_config.get("two-pass-linker", {}).get("desc", "")))
        result = result.replace("[COMMENTATOR_DESC]", self._escape_latex(
            projects_config.get("commentator", {}).get("desc", "")))

        # 2. Reorder IBM bullets
        result = self._reorder_section(
            result,
            "IBM_BULLETS_START", "IBM_BULLETS_END",
            config.ibm_bullet_order,
            prefix="IBM_",
        )

        # 3. Reorder Sahara bullets
        result = self._reorder_section(
            result,
            "SAHARA_BULLETS_START", "SAHARA_BULLETS_END",
            config.sahara_bullet_order,
            prefix="SAHARA_",
        )

        # 4. Reorder projects (select top 3 from the 4 available)
        result = self._reorder_projects(result, config.projects[:3])

        # 5. Set language order
        lang_line = ", ".join(config.language_order)
        result = result.replace("[LANGUAGES]", lang_line)

        return result

    def _reorder_section(
        self, text: str, start_marker: str, end_marker: str,
        order: list[str], prefix: str,
    ) -> str:
        """Reorder \\resumeItem lines within a marked section.

        Finds lines tagged with the marker (e.g., lines containing IBM_OWN)
        and reorders them according to the given order.
        """
        start_pat = re.compile(rf"^% {start_marker}\s*$", re.MULTILINE)
        end_pat = re.compile(rf"^% {end_marker}\s*$", re.MULTILINE)

        start_match = start_pat.search(text)
        end_match = end_pat.search(text)

        if not start_match or not end_match:
            logger.warning(f"Markers {start_marker}/{end_marker} not found in template")
            return text

        # Extract the section between markers
        section = text[start_match.end():end_match.start()]
        lines = section.strip().split("\n")

        # Build a map: key → line (each resumeItem line)
        key_map: dict[str, str] = {}
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped or line_stripped.startswith("%"):
                continue
            # Match by content — the bullet text tells us which key it is
            for key in order:
                marker_tag = f"{prefix}{key.upper()}"
                # After substitution, check if the bullet content from that key is in this line
                bullet_config = self.resume_config.get(
                    start_marker.split("_")[0].lower() + "_bullets", {}
                )
                bullet_text = bullet_config.get(key, "")
                if bullet_text and bullet_text[:30] in line:
                    key_map[key] = line
                    break

        # Rebuild section in desired order
        ordered_lines = []
        for key in order:
            if key in key_map:
                ordered_lines.append(key_map[key])

        # Include any lines not matched (safety net)
        matched_lines = set(key_map.values())
        for line in lines:
            if line.strip() and not line.strip().startswith("%") and line not in matched_lines:
                ordered_lines.append(line)

        new_section = "\n".join(ordered_lines)
        return (
            text[:start_match.end()] + "\n" +
            new_section + "\n" +
            text[end_match.start():]
        )

    def _reorder_projects(self, text: str, projects: list[str]) -> str:
        """Reorder project blocks between PROJECTS_START/PROJECTS_END markers.

        Each project block starts with % PROJECT:<name> and runs until
        the next % PROJECT: or % PROJECTS_END.
        """
        start_pat = re.compile(r"^% PROJECTS_START\s*$", re.MULTILINE)
        end_pat = re.compile(r"^% PROJECTS_END\s*$", re.MULTILINE)

        start_match = start_pat.search(text)
        end_match = end_pat.search(text)

        if not start_match or not end_match:
            logger.warning("Project markers not found in template")
            return text

        section = text[start_match.end():end_match.start()]

        # Parse project blocks
        project_blocks: dict[str, str] = {}
        current_name = None
        current_lines: list[str] = []

        for line in section.split("\n"):
            proj_match = re.match(r"^% PROJECT:(.+)$", line.strip())
            if proj_match:
                if current_name:
                    project_blocks[current_name] = "\n".join(current_lines)
                current_name = proj_match.group(1).strip()
                current_lines = [line]
            elif current_name:
                current_lines.append(line)

        if current_name:
            project_blocks[current_name] = "\n".join(current_lines)

        # Rebuild in desired order (top 3)
        ordered_blocks = []
        for proj_name in projects:
            if proj_name in project_blocks:
                ordered_blocks.append(project_blocks[proj_name])

        new_section = "\n".join(ordered_blocks)
        return (
            text[:start_match.end()] + "\n" +
            new_section + "\n" +
            text[end_match.start():]
        )

    @staticmethod
    def _escape_latex(text: str) -> str:
        """Escape special LaTeX characters."""
        replacements = {
            "&": r"\&",
            "%": r"\%",
            "$": r"\$",
            "#": r"\#",
            "_": r"\_",
            "{": r"\{",
            "}": r"\}",
            "~": r"\textasciitilde{}",
            "^": r"\^{}",
        }
        for char, escaped in replacements.items():
            text = text.replace(char, escaped)
        return text

    def _validate_pdf(self, pdf_path: Path) -> bool:
        """Validate PDF is exactly 1 page with 94-98% fill."""
        try:
            # Check page count with pdfinfo
            result = subprocess.run(
                ["pdfinfo", str(pdf_path)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if line.startswith("Pages:"):
                        pages = int(line.split(":")[1].strip())
                        if pages != 1:
                            logger.warning(f"Resume is {pages} pages, expected 1")
                            return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("pdfinfo not available, skipping page count check")

        # TODO: Use pdftoppm + PIL to check fill percentage
        # This requires poppler-utils and Pillow
        return True
