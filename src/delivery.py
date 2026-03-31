"""Delivery: sends results via Telegram and marks Todoist tasks complete."""

import shutil
import tempfile
from pathlib import Path

from .dispatcher import DispatchResult
from .poller import Poller
from .state import TaskState
from .telegram import TelegramBot


class Delivery:
    def __init__(self, poller: Poller, state: TaskState, telegram: TelegramBot):
        self.poller = poller
        self.state = state
        self.telegram = telegram

    async def _upload_output_files(self, output_files: list[str], nickname: str) -> None:
        """Upload output files via Telegram. Zips if there are many."""
        if not output_files:
            return
        if len(output_files) == 1:
            try:
                await self.telegram.send_file(output_files[0])
            except Exception as e:
                print(f"    [warn] Failed to upload file: {e}")
            return
        # Multiple files — zip them
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / f"{nickname}-output"
                shutil.make_archive(str(zip_path), "zip", Path(output_files[0]).parent)
                await self.telegram.send_file(f"{zip_path}.zip", caption=f"{nickname} output files")
        except Exception as e:
            print(f"    [warn] Failed to zip/upload files: {e}")
            # Fall back to sending individually
            for f in output_files[:10]:
                try:
                    await self.telegram.send_file(f)
                except Exception:
                    pass

    async def send_for_review(self, task_id: str, task_content: str, result: DispatchResult,
                              plan_context: dict | None = None) -> None:
        """Send results via Telegram and wait for human to approve before completing."""
        nickname = self.state.get_nickname(task_id)
        await self.telegram.send_result(
            task_id=task_id,
            task_title=task_content,
            success=result.success,
            summary=result.summary,
            output_files=result.output_files,
            cost_usd=result.cost_usd,
            nickname=nickname,
        )

        # Upload output files via Telegram
        await self._upload_output_files(result.output_files, nickname)

        if result.success:
            self.state.set_awaiting_review(task_id, plan_context)
        else:
            self.state.set_failed(task_id, result.summary)

    def complete(self, task_id: str) -> None:
        """Mark task as complete (Todoist + state, or state-only for telegram tasks)."""
        entry = self.state.get(task_id)
        if not entry or entry.get("source") != "telegram":
            self.poller.complete_task(task_id)
        self.state.set_completed(task_id)
