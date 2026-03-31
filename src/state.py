"""Persistent task state tracking."""

import json
import re
import time
from pathlib import Path
from typing import Any

# Task lifecycle:
#   new → planning → awaiting_approval → processing → awaiting_review → completed
#                  ↘ conversing ↗       ↘ conversing ↗  → failed
#                                                       → waiting_for_human → processing → ...

TERMINAL_STATUSES = {"completed", "failed"}
SKIP_STATUSES = {"completed", "failed", "waiting_for_human", "awaiting_approval", "awaiting_review", "conversing", "error", "pending_local", "processing_local"}

# Multi-word phrases stripped from the front of task content when generating nicknames
_STRIP_PHRASES = [
    "set up", "look into", "figure out", "put together", "come up with",
    "work on", "fill out", "fill in", "sign up", "log into", "look up",
]
_STRIP_VERBS = {
    "create", "build", "write", "draft", "research", "find", "make", "send",
    "prepare", "update", "fix", "review", "check", "get", "add", "do",
    "finish", "complete", "start", "begin", "help", "compare", "investigate",
    "explore", "analyze", "determine", "setup", "implement", "book",
}
_FILLER = {
    "a", "an", "the", "for", "about", "of", "to", "with", "on", "in",
    "my", "our", "their", "some", "this", "that", "me", "top", "best",
    "new", "up", "good", "great", "nice", "from", "into", "all",
}


def _generate_nickname(task_content: str) -> str:
    """Generate a short human-friendly nickname from task content.

    Examples:
        "Build a slide deck about Q1 results" -> "slide-deck"
        "Research competitor pricing strategies" -> "competitor-pricing"
        "Draft quarterly report" -> "quarterly-report"
    """
    text = re.sub(r"[^\w\s-]", "", task_content.lower()).strip()
    # Strip leading multi-word phrases first
    for phrase in _STRIP_PHRASES:
        if text.startswith(phrase):
            text = text[len(phrase):].strip()
            break
    words = text.split()
    # Strip leading verbs and filler words
    while words and words[0] in (_STRIP_VERBS | _FILLER):
        words.pop(0)
    # Remove filler from remaining words; also drop pure numbers
    words = [w for w in words if w not in _FILLER and not w.isdigit()]
    if not words:
        # Fallback: first non-filler word from original
        words = [w for w in task_content.lower().split() if w.strip(".,!?") not in _FILLER][:1] or ["task"]
    # Take 2 words for a recognizable name
    nickname = "-".join(words[:2])
    return nickname


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

    def assign_nickname(self, task_id: str, task_content: str) -> str:
        """Generate and store a unique nickname for a task."""
        base = _generate_nickname(task_content)
        existing = {
            entry.get("nickname")
            for tid, entry in self._data.items()
            if tid != task_id
        }
        nickname = base
        counter = 2
        while nickname in existing:
            nickname = f"{base}-{counter}"
            counter += 1
        if task_id in self._data:
            self._data[task_id]["nickname"] = nickname
            self._save()
        return nickname

    def get_nickname(self, task_id: str) -> str:
        """Get the nickname for a task, falling back to short ID."""
        entry = self._data.get(task_id)
        if entry:
            return entry.get("nickname", task_id[:8])
        return task_id[:8]

    def find_by_thread(self, thread_id: int) -> str | None:
        """Find a task ID by its Telegram forum thread_id."""
        for tid, entry in self._data.items():
            if entry.get("thread_id") == thread_id:
                return tid
        return None

    def get_thread_id(self, task_id: str) -> int | None:
        """Get the forum thread_id for a task."""
        entry = self._data.get(task_id)
        return entry.get("thread_id") if entry else None

    def find_by_nickname(self, target: str, task_ids: list[str] | None = None) -> list[str]:
        """Find task IDs matching a nickname (exact first, then prefix).

        If task_ids is given, only search within that subset.
        """
        candidates = task_ids if task_ids is not None else list(self._data.keys())
        # Exact match
        exact = [
            tid for tid in candidates
            if self._data.get(tid, {}).get("nickname") == target
        ]
        if exact:
            return exact
        # Prefix match
        return [
            tid for tid in candidates
            if self._data.get(tid, {}).get("nickname", "").startswith(target)
        ]

    def _carry_forward(self, task_id: str, entry: dict[str, Any]) -> None:
        """Preserve nickname, task_content, and source when overwriting a state entry."""
        old = self._data.get(task_id, {})
        for key in ("nickname", "task_content", "source", "execution_target", "result_summary", "thread_id"):
            if key not in entry and key in old:
                entry[key] = old[key]

    def status(self, task_id: str) -> str | None:
        entry = self._data.get(task_id)
        return entry["status"] if entry else None

    def is_finished(self, task_id: str) -> bool:
        return self.status(task_id) in TERMINAL_STATUSES

    def set_planning(self, task_id: str, task_content: str = "") -> None:
        entry: dict[str, Any] = {"status": "planning"}
        if task_content:
            entry["task_content"] = task_content
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def set_awaiting_approval(self, task_id: str, plan_text: str,
                              task_content: str = "") -> None:
        entry: dict[str, Any] = {
            "status": "awaiting_approval",
            "plan": plan_text,
        }
        if task_content:
            entry["task_content"] = task_content
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def set_processing(self, task_id: str, plan_context: dict, human_completed: str = "",
                        task_content: str = "") -> None:
        """Mark task as processing and store plan context for resume."""
        entry: dict[str, Any] = {
            "status": "processing",
            "plan": plan_context["plan"],
            "use_user_browser": plan_context.get("use_user_browser", False),
            "output_dir": plan_context.get("output_dir"),
            "processing_started_at": time.time(),
        }
        if task_content:
            entry["task_content"] = task_content
        if human_completed:
            entry["human_completed"] = human_completed
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def get_processing_tasks(self) -> dict[str, dict]:
        """Return all tasks currently in processing state."""
        return {
            task_id: entry
            for task_id, entry in self._data.items()
            if entry.get("status") == "processing"
        }

    def get_telegram_processing_tasks(self) -> dict[str, dict]:
        """Return telegram-sourced tasks currently in processing state."""
        return {
            task_id: entry
            for task_id, entry in self._data.items()
            if entry.get("status") == "processing" and entry.get("source") == "telegram"
        }

    def set_pending_local(self, task_id: str, plan_context: dict,
                          human_completed: str = "", task_content: str = "") -> None:
        """Mark task as waiting for local worker to pick up."""
        entry: dict[str, Any] = {
            "status": "pending_local",
            "plan": plan_context["plan"],
            "use_user_browser": plan_context.get("use_user_browser", False),
            "output_dir": plan_context.get("output_dir"),
            "pending_local_since": time.time(),
        }
        if task_content:
            entry["task_content"] = task_content
        if human_completed:
            entry["human_completed"] = human_completed
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def get_pending_local_tasks(self) -> dict[str, dict]:
        """Return tasks waiting for local worker."""
        return {
            task_id: entry
            for task_id, entry in self._data.items()
            if entry.get("status") == "pending_local"
        }

    def set_awaiting_review(self, task_id: str, plan_context: dict | None = None,
                            result_summary: str = "") -> None:
        entry: dict[str, Any] = {"status": "awaiting_review"}
        if plan_context:
            entry["plan"] = plan_context["plan"]
            entry["use_user_browser"] = plan_context.get("use_user_browser", False)
            entry["output_dir"] = plan_context.get("output_dir")
        if result_summary:
            entry["result_summary"] = result_summary
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def set_completed(self, task_id: str) -> None:
        self._data[task_id] = {"status": "completed"}
        self._save()

    def is_waiting(self, task_id: str) -> bool:
        return self.status(task_id) == "waiting_for_human"

    def set_waiting_for_human(self, task_id: str, message: str, plan_context: dict | None = None) -> None:
        entry: dict[str, Any] = {
            "status": "waiting_for_human",
            "waiting_message": message,
        }
        if plan_context:
            entry["plan"] = plan_context["plan"]
            entry["use_user_browser"] = plan_context.get("use_user_browser", False)
            entry["output_dir"] = plan_context.get("output_dir")
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def set_conversing(self, task_id: str, from_status: str, plan_context: dict,
                       task_content: str, conversation_history: list[dict] | None = None) -> None:
        """Enter conversing state — user is chatting with LLM to refine requirements."""
        entry: dict[str, Any] = {
            "status": "conversing",
            "from_status": from_status,  # "awaiting_approval" or "awaiting_review"
            "plan": plan_context.get("plan", ""),
            "use_user_browser": plan_context.get("use_user_browser", False),
            "output_dir": plan_context.get("output_dir"),
            "task_content": task_content,
            "conversation_history": conversation_history or [],
        }
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
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

    def set_error(self, task_id: str, error: str, phase: str, plan_context: dict | None = None) -> None:
        """Set task to error state — retryable via Telegram.

        phase: "planning" or "processing" — determines what state to resume from on retry.
        """
        entry: dict[str, Any] = {
            "status": "error",
            "error": error,
            "phase": phase,
        }
        if plan_context:
            entry["plan"] = plan_context.get("plan", "")
            entry["use_user_browser"] = plan_context.get("use_user_browser", False)
            entry["output_dir"] = plan_context.get("output_dir")
        self._carry_forward(task_id, entry)
        self._data[task_id] = entry
        self._save()

    def set_failed(self, task_id: str, error: str = "") -> None:
        entry: dict[str, Any] = {"status": "failed"}
        if error:
            entry["error"] = error
        self._data[task_id] = entry
        self._save()
