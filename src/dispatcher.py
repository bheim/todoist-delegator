"""Dispatcher: runs the Claude Agent SDK to execute tasks."""

import asyncio
import os
import shutil as _shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from todoist_api_python.api import TodoistAPI

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)

from .config import Config
from .router import RoutedTask, WEB_FORM_HEADLESS_PROMPT, WEB_FORM_TOOLS


@dataclass
class DispatchResult:
    success: bool
    summary: str
    output_files: list[str]
    cost_usd: float
    needs_human: str = ""  # non-empty if agent needs human intervention


class Dispatcher:
    def __init__(self, config: Config):
        self.config = config
        self.todoist = TodoistAPI(config.todoist_api_token)

    def _flatten_comments(self, task_id: str) -> list:
        comments = []
        for page in self.todoist.get_comments(task_id=task_id):
            comments.extend(page)
        return comments

    def _wait_for_go(self, task_id: str, known_ids: set[str], timeout: float = 120.0) -> bool:
        """Poll for a 'go' reply. Returns True if confirmed, False on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            for c in self._flatten_comments(task_id):
                if c.id not in known_ids and "go" in c.content.strip().lower():
                    return True
        return False

    async def _confirm_chrome_close(self, task_id: str, routed: RoutedTask) -> RoutedTask:
        """Ask user to close Chrome and wait for confirmation. Falls back to headless on timeout."""
        self.todoist.add_comment(
            task_id=task_id,
            content="⚠️ I need to use your Chrome session. Please close Chrome and reply `go` when ready.",
        )

        known_ids = {c.id for c in self._flatten_comments(task_id)}
        confirmed = await asyncio.get_event_loop().run_in_executor(
            None, self._wait_for_go, task_id, known_ids
        )

        if not confirmed:
            self.todoist.add_comment(
                task_id=task_id,
                content="⏰ No confirmation received. Falling back to headless mode.",
            )
            return RoutedTask(
                task_type=routed.task_type,
                tools=WEB_FORM_TOOLS,
                system_prompt=WEB_FORM_HEADLESS_PROMPT,
                agent_prompt=routed.agent_prompt,
                use_user_browser=False,
            )

        return routed

    async def dispatch(self, task_id: str, routed: RoutedTask) -> DispatchResult:
        """Run the agent for a routed task."""
        # If web_form with user browser, confirm Chrome is closed first
        if routed.task_type == "web_form" and routed.use_user_browser:
            routed = await self._confirm_chrome_close(task_id, routed)

        # Create per-task working directory
        task_dir = Path(self.config.working_dir) / f"task-{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)

        def log_stderr(line: str) -> None:
            print(f"    [agent stderr] {line.rstrip()}", file=sys.stderr)

        # For web_form tasks: kill any existing headless daemon so --headed works
        agent_env = {}
        if routed.task_type == "web_form":
            import shutil
            import subprocess
            if shutil.which("agent-browser"):
                subprocess.run(["agent-browser", "close"], capture_output=True)
            os.environ["AGENT_BROWSER_HEADED"] = "true"
            agent_env["AGENT_BROWSER_HEADED"] = "true"
            agent_env["TODOIST_API_TOKEN"] = self.config.todoist_api_token
        else:
            os.environ.pop("AGENT_BROWSER_HEADED", None)

        options = ClaudeAgentOptions(
            model=self.config.agent_model,
            system_prompt=routed.system_prompt + f"\n\nTask ID for Todoist comments: {task_id}",
            allowed_tools=routed.tools,
            permission_mode="bypassPermissions",
            max_turns=self.config.agent_max_turns,
            cwd=str(task_dir),
            stderr=log_stderr,
            env=agent_env,
        )

        assistant_texts: list[str] = []
        cost_usd = 0.0
        is_error = False

        async for message in query(prompt=routed.agent_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        assistant_texts.append(block.text)
                        print(f"    [agent] {block.text[:120]}")
            elif isinstance(message, SystemMessage):
                print(f"    [system] {message.subtype}: {message.data}")
            elif isinstance(message, ResultMessage):
                cost_usd = message.total_cost_usd or 0.0
                is_error = message.is_error
                if message.result:
                    print(f"    [result] {message.result[:200]}")

        # Collect output files
        output_files = []
        if task_dir.exists():
            for f in task_dir.rglob("*"):
                if f.is_file():
                    output_files.append(str(f))

        # Copy output files to user-specified directory if provided
        if routed.output_dir and output_files:
            dest = Path(os.path.expanduser(routed.output_dir)).resolve()
            dest.mkdir(parents=True, exist_ok=True)
            copied_files = []
            for f in output_files:
                src = Path(f)
                target = dest / src.name
                _shutil.copy2(str(src), str(target))
                copied_files.append(str(target))
            output_files = copied_files

        summary = assistant_texts[-1] if assistant_texts else "(no output)"

        # Check if agent needs human intervention
        needs_human = ""
        for text in assistant_texts:
            if "NEEDS_HUMAN:" in text:
                needs_human = text.split("NEEDS_HUMAN:", 1)[1].strip()
                break

        return DispatchResult(
            success=not is_error and not needs_human,
            summary=summary,
            output_files=output_files,
            cost_usd=cost_usd,
            needs_human=needs_human,
        )
