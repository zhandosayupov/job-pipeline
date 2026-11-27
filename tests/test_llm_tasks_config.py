"""Every LLM task the code calls has an entry in config.example/llm.yaml and a prompt file."""
import re

import yaml

from jobpipe.config.settings import LLMConfig

from .conftest import REPO


def _called_tasks() -> set[str]:
    out = set()
    for p in (REPO / "src" / "jobpipe").rglob("*.py"):
        out |= set(re.findall(r"\.complete\(\s*\"([a-z_]+)\"", p.read_text(encoding="utf-8")))
    return out


def test_every_called_task_is_configured_with_a_prompt():
    cfg = LLMConfig.model_validate(yaml.safe_load((REPO / "config.example" / "llm.yaml").read_text(encoding="utf-8")))
    called = _called_tasks()
    assert called, "no router.complete calls found"
    missing = called - set(cfg.tasks)
    assert not missing, f"tasks used in code but missing in config.example/llm.yaml: {sorted(missing)}"
    for name in called:
        version = cfg.tasks[name].prompt_version
        assert (REPO / "prompts" / name / f"{version}.md").exists(), f"prompts/{name}/{version}.md missing"
