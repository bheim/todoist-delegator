"""Persistent task state tracking."""

import json
from pathlib import Path
from typing import Any

# Task lifecycle:
#   new → planning → awaiting_approval → processing → awaiting_review → completed
#                  ↘ conversing ↗       ↘ conversing ↗  → failed
#                                                       → waiting_for_human → processing → ...

TERMINAL_STATUSES = {"completed", "failed"}
SKIP_STATUSES = {"completed", "failed", "waiting_for_human", "awaiting_approval", "awaiting_review", "conversing"}


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

    def set_planning(self, task_id: str) -> None:
        self._data[task_id] = {"status": "planning"}
        self._save()

    def set_awaiting_approval(self, task_id: str, plan_text: str) -> None:
        self._data[task_id] = {
            "status": "awaiting_approval",
            "plan": plan_text,
        }
        self._save()

    def set_processing(self, task_id: str, plan_context: dict, human_completed: str = "") -> None:
        """Mark task as processing and store plan context for resume."""
        entry = {
            "status": "processing",
            "plan": plan_context["plan"],
            "use_user_browser": plan_context.get("use_user_browser", False),
            "output_dir": plan_context.get("output_dir"),
        }
        if human_completed:
            entry["human_completed"] = human_completed
        self._data[task_id] = entry
        self._save()

    def set_awaiting_review(self, task_id: str, plan_context: dict | None = None) -> None:
        entry: dict[str, Any] = {"status": "awaiting_review"}
        if plan_context:
            entry["plan"] = plan_context["plan"]
            entry["use_user_browser"] = plan_context.get("use_user_browser", False)
            entry["output_dir"] = plan_context.get("output_dir")
        self._data[task_id] = entry
        self._save()

    def set_completed(self, task_id: str) -> None:
        self._data[task_id] = {"status": "completed"}
        self._save()

    def is_waiting(self, task_id: str) -> bool:
        return self.status(task_id) == "waiting_for_human"

    def set_waiting_for_human(self, task_id: str, message: str, plan_context: dict | None = None) -> None:
        entry = {
            "status": "waiting_for_human",
            "waiting_message": message,
        }
        if plan_context:
            entry["plan"] = plan_context["plan"]
            entry["use_user_browser"] = plan_context.get("use_user_browser", False)
            entry["output_dir"] = plan_context.get("output_dir")
        self._data[task_id] = entry
        self._save()

    def set_conversing(self, task_id: str, from_status: str, plan_context: dict,
                       task_content: str, conversation_history: list[dict] | None = None) -> None:
        """Enter conversing state — user is chatting with LLM to refine requirements."""
        self._data[task_id] = {
            "status": "conversing",
            "from_status": from_status,  # "awaiting_approval" or "awaiting_review"
            "plan": plan_context.get("plan", ""),
            "use_user_browser": plan_context.get("use_user_browser", False),
            "output_dir": plan_context.get("output_dir"),
            "task_content": task_content,
            "conversation_history": conversation_history or [],
        }
        self._save()

    def append_conversation(self, task_id: str, role: str, content) -> None:
        """Append a message to the conversation history.

        content can be a plain string or a list of content blocks (for assistant
        turns that include tool use like web_search).
        """
        entry = self._data.get(task_id)
        if entry and entry.get("status") == "conversing":
            entry.setdefault("conversation_history", []).append(
                {"role": role, "content": content}
            )
            self._save()

    def set_failed(self, task_id: str, error: str = "") -> None:
        entry: dict[str, Any] = {"status": "failed"}
        if error:
            entry["error"] = error
        self._data[task_id] = entry
        self._save()
