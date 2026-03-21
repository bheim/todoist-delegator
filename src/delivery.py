"""Delivery: posts results as Todoist comments and marks tasks complete."""

from .dispatcher import DispatchResult
from .poller import Poller
from .state import TaskState


class Delivery:
    def __init__(self, poller: Poller, state: TaskState):
        self.poller = poller
        self.state = state

    def deliver(self, task_id: str, task_content: str, result: DispatchResult) -> None:
        """Post results as a Todoist comment and complete the task if successful."""
        status = "Completed" if result.success else "Failed"

        file_list = "\n".join(f"- `{f}`" for f in result.output_files) if result.output_files else "(none)"

        comment = (
            f"🤖 **Delegator Result: {status}**\n\n"
            f"**Summary:**\n{result.summary}\n\n"
            f"**Output files:**\n{file_list}\n\n"
            f"**Cost:** ${result.cost_usd:.4f}"
        )

        self.poller.add_comment(task_id, comment)

        if result.success:
            self.poller.complete_task(task_id)
            self.state.set_completed(task_id)
        else:
            self.state.set_failed(task_id, result.summary)
