"""Persistent task state tracking."""

import json
from pathlib import Path
from typing import Any

# Task lifecycle:
#   new → clarifying → processing → completed
#                                  → failed
#                                  → waiting_for_human → processing → ...

TERMINAL_STATUSES = {"completed", "failed"}
SKIP_STATUSES = {"completed", "failed", "waiting_for_human"}


class TaskState:
    def __init__(self, state_file: str | Path):
        self._path = Path(state_file)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._data.get(task_id)

    def status(self, task_id: str) -> str | None:
        entry = self._data.get(task_id)
        return entry["status"] if entry else None

    def is_finished(self, task_id: str) -> bool:
        return self.status(task_id) in TERMINAL_STATUSES

    def set_clarifying(self, task_id: str) -> None:
        self._data[task_id] = {"status": "clarifying"}
        self._save()

    def set_processing(self, task_id: str, clarification: dict) -> None:
        """Mark task as processing and store clarification data for resume."""
        # Store a serializable copy (strip the DelegatedTask object)
        serializable = {
            "questions": clarification["questions"],
            "answers": clarification["answers"],
            "skipped": clarification["skipped"],
            "use_user_browser": clarification.get("use_user_browser", False),
        }
        self._data[task_id] = {
            "status": "processing",
            "clarification": serializable,
        }
        self._save()

    def set_completed(self, task_id: str) -> None:
        entry = self._data.get(task_id, {})
        entry["status"] = "completed"
        entry.pop("clarification", None)
        self._data[task_id] = entry
        self._save()

    def is_waiting(self, task_id: str) -> bool:
        return self.status(task_id) == "waiting_for_human"

    def set_waiting_for_human(self, task_id: str, message: str, clarification: dict | None = None) -> None:
        entry = self._data.get(task_id, {})
        entry["status"] = "waiting_for_human"
        entry["waiting_message"] = message
        if clarification:
            entry["clarification"] = clarification
        self._data[task_id] = entry
        self._save()

    def set_failed(self, task_id: str, error: str = "") -> None:
        entry = self._data.get(task_id, {})
        entry["status"] = "failed"
        entry.pop("clarification", None)
        if error:
            entry["error"] = error
        self._data[task_id] = entry
        self._save()
