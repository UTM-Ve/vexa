"""synergy_tasks — the Synergy-only task-breakdown stage.

Synergy-only: this package lives outside the open-core carve and imports nothing from ``core/``. It
reads the artifacts the meeting copilot already writes into a workspace and writes tasks back into
the same workspace, so it can be developed, deployed, and switched off without touching core.
"""
from synergy_tasks.breakdown import (
    DEFAULT_TASK_RULES,
    UNASSIGNED,
    break_down_meeting,
    normalize_due,
    parse_tasks,
    resolve_owner,
)
from synergy_tasks.config import TaskConfig, load_task_config
from synergy_tasks.workspace import Meeting, find_meetings, render_task_entity, upsert_task_file

__all__ = [
    "DEFAULT_TASK_RULES", "UNASSIGNED", "Meeting", "TaskConfig", "break_down_meeting",
    "find_meetings", "load_task_config", "normalize_due", "parse_tasks", "render_task_entity",
    "resolve_owner", "upsert_task_file",
]
