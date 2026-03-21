"""Todoist poller for tasks with the @delegate label."""

from dataclasses import dataclass, field
from pathlib import Path

import httpx
from todoist_api_python.api import TodoistAPI

from .config import Config
from .state import TaskState


@dataclass
class DelegatedTask:
    task_id: str
    content: str
    description: str
    project_name: str
    labels: list[str]
    comments: list[str]
    attachments: list[str] = field(default_factory=list)  # local paths to downloaded files


class Poller:
    def __init__(self, config: Config, state: TaskState):
        self.api = TodoistAPI(config.todoist_api_token)
        self.api_token = config.todoist_api_token
        self.label_name = config.delegate_label_name
        self.working_dir = config.working_dir
        self.state = state

    def _flatten(self, paginator) -> list:
        """Flatten a paginated iterator into a single list."""
        items = []
        for page in paginator:
            items.extend(page)
        return items

    def _download_attachments(self, task_id: str, comments: list) -> list[str]:
        """Download file attachments from comments into the task working dir."""
        task_dir = Path(self.working_dir) / f"task-{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for comment in comments:
            att = comment.attachment
            if att is None:
                continue
            url = att.file_url
            filename = att.file_name
            if not url or not filename:
                continue

            dest = task_dir / filename
            if dest.exists() and dest.stat().st_size > 1000:
                downloaded.append(str(dest))
                continue

            try:
                with httpx.Client(follow_redirects=True) as client:
                    resp = client.get(
                        url,
                        headers={"Authorization": f"Bearer {self.api_token}"},
                        timeout=60.0,
                    )
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
                    downloaded.append(str(dest))
            except Exception as e:
                print(f"    [warn] Failed to download {filename}: {e}")

        return downloaded

    def poll(self) -> list[DelegatedTask]:
        """Poll for tasks matching the delegate label, skipping finished ones."""
        print("  Polling Todoist...", flush=True)
        tasks = self._flatten(self.api.get_tasks(label=self.label_name))
        result = []

        for task in tasks:
            if self.state.status(task.id) in ("completed", "failed", "waiting_for_human"):
                continue

            print(f"  Fetching details for: {task.content}", flush=True)

            # Get project name
            project_name = ""
            if task.project_id:
                try:
                    project = self.api.get_project(task.project_id)
                    project_name = project.name
                except Exception:
                    pass

            # Get comments (full objects for attachment handling)
            raw_comments = []
            try:
                raw_comments = self._flatten(self.api.get_comments(task_id=task.id))
            except Exception:
                pass

            comment_texts = [c.content for c in raw_comments]

            # Download any file attachments into the task workspace
            print(f"  Downloading attachments...", flush=True)
            attachments = self._download_attachments(task.id, raw_comments)

            result.append(
                DelegatedTask(
                    task_id=task.id,
                    content=task.content,
                    description=task.description or "",
                    project_name=project_name,
                    labels=task.labels,
                    comments=comment_texts,
                    attachments=attachments,
                )
            )

        return result

    def complete_task(self, task_id: str) -> None:
        """Mark a task as complete."""
        self.api.complete_task(task_id=task_id)

    def add_comment(self, task_id: str, content: str) -> None:
        """Add a comment to a task."""
        self.api.add_comment(task_id=task_id, content=content)
