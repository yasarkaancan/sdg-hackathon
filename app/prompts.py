"""Loads prompt templates from /prompts/*.md.

File format (human-readable markdown):

    # Title + design notes (ignored by the loader)
    ## SYSTEM PROMPT
    ...system text...
    ## USER PROMPT TEMPLATE
    ...user text with {placeholders}...

Placeholders ({lower_snake_case}) are substituted with a regex (not str.format),
so the JSON schemas inside the prompts can use literal { } braces safely.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
SYSTEM_MARK = "## SYSTEM PROMPT"
USER_MARK = "## USER PROMPT TEMPLATE"

PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

LANGUAGE_NAMES = {"en": "English", "tr": "Turkish (Türkçe)"}


def load(name: str) -> tuple[str, str]:
    """Read on every call (6 small files) so prompt edits apply without a restart."""
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    if SYSTEM_MARK not in text or USER_MARK not in text:
        raise ValueError(f"prompts/{name}.md must contain '{SYSTEM_MARK}' and '{USER_MARK}' headings")
    _, rest = text.split(SYSTEM_MARK, 1)
    system, user = rest.split(USER_MARK, 1)
    return system.strip(), user.strip()


def render(name: str, **values: str) -> tuple[str, str]:
    """Fill {placeholders} in ONE pass, so user text that happens to contain
    "{cv_text}" can never pull other content into the prompt."""
    system, user = load(name)

    def fill(template: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)

    return fill(system), fill(user)
