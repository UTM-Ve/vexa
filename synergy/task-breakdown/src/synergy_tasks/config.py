"""config.py — the governed knobs, resolved per workspace.

A workspace steers this stage through a VISIBLE, git-tracked file: ``agents/tasks.md`` (YAML
frontmatter + a natural-language body merged into the prompt), the same prompt-only governance the
meeting copilot gives ``agents/meeting.md``. The template lives at ``../../templates/tasks.md``.

Parsing is TOLERANT: a missing file, missing/partial frontmatter, or malformed YAML each fall back
PER-KEY to the code defaults, and the body (if any) is always taken as steering. Env wins over the
file for the on/off switch — an operator must be able to stop the stage without editing user data.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from synergy_tasks.breakdown import DEFAULT_TASK_RULES

log = logging.getLogger("synergy_tasks")

# Where the governed config lives INSIDE a workspace (visible, git-tracked).
TASKS_CONFIG_PATH = "agents/tasks.md"

_FRONTMATTER = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def default_model() -> str:
    """``VEXA_TASKS_MODEL`` → ``VEXA_MEETING_MODEL`` → ``VEXA_LLM_MODEL`` → "" (the adapter's own
    default). A free string the provider adapter interprets — no vendor name lives in code."""
    return (os.environ.get("VEXA_TASKS_MODEL") or os.environ.get("VEXA_MEETING_MODEL")
            or os.environ.get("VEXA_LLM_MODEL") or "")


@dataclass(frozen=True)
class TaskConfig:
    """The resolved knobs for one workspace (every field has a code default)."""

    enabled: bool = True
    model: str = field(default_factory=default_model)
    task_rules: str = DEFAULT_TASK_RULES
    steering: str = ""
    commit: bool = True


def as_bool(val: object, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        if val.strip().lower() in {"true", "yes", "1", "on"}:
            return True
        if val.strip().lower() in {"false", "no", "0", "off"}:
            return False
    return default


def load_task_config(workspace: Path) -> TaskConfig:
    """Read ``<workspace>/agents/tasks.md`` → the resolved ``TaskConfig``, PER-KEY fallback to the
    code defaults (absent file ⇒ all defaults, so a workspace that has never heard of this stage
    still gets a breakdown). ``VEXA_MEETING_TASKS`` is the operator kill-switch and always wins;
    ``VEXA_TASKS_COMMIT`` likewise for whether the writes are committed."""
    enabled, model, rules, steering = True, "", DEFAULT_TASK_RULES, ""
    commit = True
    path = Path(workspace) / TASKS_CONFIG_PATH
    if path.exists():
        try:
            text = path.read_text()
        except OSError:
            text = ""
        match = _FRONTMATTER.match(text)
        body = text.strip()
        data: dict = {}
        if match:
            body = match.group(2).strip()
            try:
                parsed = yaml.safe_load(match.group(1))
                data = parsed if isinstance(parsed, dict) else {}
            except yaml.YAMLError:
                log.warning("%s: malformed YAML frontmatter — using defaults", path)
        enabled = as_bool(data.get("enabled"), True)
        commit = as_bool(data.get("commit"), True)
        if isinstance(data.get("model"), str) and data["model"].strip():
            model = data["model"].strip()
        if isinstance(data.get("task_rules"), str) and data["task_rules"].strip():
            rules = data["task_rules"].strip()
        steering = body
    env_gate = os.environ.get("VEXA_MEETING_TASKS")
    if env_gate is not None:
        enabled = as_bool(env_gate, enabled)
    env_commit = os.environ.get("VEXA_TASKS_COMMIT")
    if env_commit is not None:
        commit = as_bool(env_commit, commit)
    return TaskConfig(enabled=enabled, model=model or default_model(), task_rules=rules,
                      steering=steering, commit=commit)
