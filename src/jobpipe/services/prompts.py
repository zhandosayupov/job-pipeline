"""Versioned prompt files: prompts/<task>/<version>.md with `## system` and `## user` sections.

Variables are written as {{name}}; a missing variable is an error, so a prompt never goes out
with a hole in it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VAR = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_SECTION = re.compile(r"^##\s+(system|user)\s*$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class Prompt:
    task: str
    version: str
    system: str
    user: str


class PromptError(ValueError):
    pass


class PromptStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def load(self, task: str, version: str) -> Prompt:
        path = self.root / task / f"{version}.md"
        if not path.exists():
            raise PromptError(f"prompt not found: {path}")
        text = path.read_text(encoding="utf-8")
        parts = _SECTION.split(text)
        sections: dict[str, str] = {}
        for i in range(1, len(parts) - 1, 2):
            sections[parts[i].lower()] = parts[i + 1].strip()
        if "user" not in sections:
            raise PromptError(f"{path}: needs a '## user' section")
        return Prompt(task, version, sections.get("system", ""), sections["user"])

    def render(self, task: str, version: str, variables: dict[str, object]) -> tuple[Prompt, list[dict[str, str]]]:
        prompt = self.load(task, version)
        messages = []
        if prompt.system:
            messages.append({"role": "system", "content": fill(prompt.system, variables)})
        messages.append({"role": "user", "content": fill(prompt.user, variables)})
        return prompt, messages


def fill(template: str, variables: dict[str, object]) -> str:
    missing = sorted({m.group(1) for m in _VAR.finditer(template)} - set(variables))
    if missing:
        raise PromptError(f"missing prompt variables: {missing}")
    return _VAR.sub(lambda m: str(variables[m.group(1)]), template)
