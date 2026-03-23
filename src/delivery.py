"""Delivery: sends results via Telegram and marks Todoist tasks complete."""

from .dispatcher import DispatchResult
from .poller import Poller
from .state import TaskState
from .telegram import TelegramBot


class Delivery:
    def __init__(self, poller: Poller, state: TaskState, telegram: TelegramBot):
        self.poller = poller
        self.state = state
        self.telegram = telegram

    async def send_for_review(self, task_id: str, task_content: str, result: DispatchResult,
                              plan_context: dict | None = None) -> None:
        """Send results via Telegram and wait for human to approve before completing."""
        await self.telegram.send_result(
            task_id=task_id,
            task_title=task_content,
            success=result.success,
            summary=result.summary,
            output_files=result.output_files,
            cost_usd=result.cost_usd,
        )

        if result.success:
            self.state.set_awaiting_review(task_id, plan_context)
        else:
            self.state.set_failed(task_id, result.summary)

    def complete(self, task_id: str) -> None:
        """Mark task as complete in Todoist after human approval."""
        self.poller.complete_task(task_id)
        self.state.set_completed(task_id)
