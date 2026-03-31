"""Local worker: polls VPS for pending_local tasks and executes them on this machine.

Usage:
    VPS_HOST=5.78.71.233 python3 -m src.local_worker

The VPS handles all Telegram communication, Todoist polling, planning, and chatbot.
This worker only handles agent execution — the expensive part that benefits from
local resources (browser, GPU, etc.).

Communication with VPS is via SSH (reads/writes state.json, rsyncs output files).
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, load_config
from .poller import DelegatedTask
from .router import Router
from .dispatcher import Dispatcher
from .telegram import TelegramBot

POLL_INTERVAL = 15  # seconds between VPS state checks
VPS_USER = "delegator"
VPS_BASE = f"/home/{VPS_USER}/todoist-delegator"
VPS_STATE_PATH = f"{VPS_BASE}/agent-workspace/state.json"
VPS_WORKSPACE = f"{VPS_BASE}/agent-workspace"
VPS_HEARTBEAT_PATH = f"{VPS_BASE}/.local-heartbeat"


class RemoteState:
    """Read/write VPS state.json via SSH."""

    def __init__(self, vps_host: str):
        self.vps_host = vps_host
        self._ssh_target = f"{VPS_USER}@{vps_host}"

    def _ssh(self, command: str, input_data: str | None = None) -> subprocess.CompletedProcess:
        cmd = ["ssh", "-o", "ConnectTimeout=5", self._ssh_target, command]
        return subprocess.run(cmd, capture_output=True, text=True, input=input_data)

    def read(self) -> dict:
        result = self._ssh(f"cat {VPS_STATE_PATH}")
        if result.returncode != 0:
            print(f"[warn] SSH read failed: {result.stderr.strip()}")
            return {}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}

    def write(self, data: dict) -> bool:
        json_str = json.dumps(data, indent=2)
        result = self._ssh(f"cat > {VPS_STATE_PATH}", input_data=json_str)
        return result.returncode == 0

    def update_task(self, task_id: str, updates: dict) -> bool:
        """Read state, update a single task entry, write back."""
        data = self.read()
        if task_id not in data:
            return False
        data[task_id].update(updates)
        return self.write(data)

    def heartbeat(self) -> None:
        self._ssh(f"touch {VPS_HEARTBEAT_PATH}")

    def rsync_to_vps(self, local_dir: str, task_id: str) -> None:
        """Rsync local task output to VPS workspace."""
        remote_dir = f"{VPS_WORKSPACE}/task-{task_id}/"
        subprocess.run(
            ["rsync", "-avz", f"{local_dir}/", f"{self._ssh_target}:{remote_dir}"],
            capture_output=True,
        )

    def get_pending_local_tasks(self) -> dict[str, dict]:
        data = self.read()
        return {
            tid: entry for tid, entry in data.items()
            if entry.get("status") == "pending_local"
        }


async def execute_local_task(task_id: str, entry: dict, config: Config,
                             remote: RemoteState) -> None:
    """Claim a pending_local task, run the agent, and report results back."""
    task_content = entry.get("task_content", "")
    nickname = entry.get("nickname", task_id[:8])
    print(f"[+] Claiming task {task_id} ({nickname}): {task_content}")

    # Claim it on VPS
    remote.update_task(task_id, {
        "status": "processing_local",
        "processing_started_at": time.time(),
    })

    # Set up local workspace
    task_dir = Path(config.working_dir) / f"task-{task_id}"
    task_dir.mkdir(parents=True, exist_ok=True)

    # Build task and plan context
    task = DelegatedTask(
        task_id=task_id,
        content=task_content,
        description="",
        project_name="",
        labels=[],
        comments=[],
    )
    plan_context = {
        "task": task,
        "plan": entry.get("plan", ""),
        "use_user_browser": entry.get("use_user_browser", False),
        "output_dir": entry.get("output_dir"),
    }
    human_completed = entry.get("human_completed", "")

    # Route and dispatch
    router = Router(config)
    telegram = TelegramBot(config)
    dispatcher = Dispatcher(config, telegram)

    try:
        print("    Classifying task type...")
        routed = await router.route(plan_context)

        if human_completed:
            routed.agent_prompt += (
                f"\n\n## Resume After Human Action\n"
                f"A previous agent run requested human help: \"{human_completed}\"\n"
                f"The human has confirmed they completed this action. "
                f"Continue from where the previous run left off."
            )

        print(f"    Type: {routed.task_type}")
        print("    Dispatching to agent...")
        result = await dispatcher.dispatch(task_id, routed)
        print(f"    Agent finished: success={result.success}, cost=${result.cost_usd:.4f}")

        # Rsync output files to VPS
        print("    Syncing output files to VPS...")
        remote.rsync_to_vps(str(task_dir), task_id)

        # Update VPS state with results
        thread_id = entry.get("thread_id")
        if result.needs_human:
            print(f"    Agent needs human help: {result.needs_human}")
            remote.update_task(task_id, {
                "status": "waiting_for_human",
                "waiting_message": result.needs_human,
            })
            await telegram.send_needs_human(task_id, task_content, result.needs_human, nickname=nickname, thread_id=thread_id)
        elif result.success:
            remote.update_task(task_id, {
                "status": "awaiting_review",
                "result_summary": result.summary,
            })
            await telegram.send_result(
                task_id=task_id,
                task_title=task_content,
                success=True,
                summary=result.summary,
                output_files=result.output_files,
                cost_usd=result.cost_usd,
                nickname=nickname,
                thread_id=thread_id,
            )
            # Upload files via Telegram
            for f in result.output_files:
                try:
                    await telegram.send_file(f, thread_id=thread_id)
                except Exception:
                    pass
        else:
            remote.update_task(task_id, {
                "status": "error",
                "error": result.summary,
                "phase": "processing",
            })
            await telegram.send_error(task_id, task_content, result.summary, nickname=nickname, thread_id=thread_id)

        print(f"[+] Done with {nickname}")

    except Exception as e:
        print(f"[!] Error executing task {task_id}: {e}")
        remote.update_task(task_id, {
            "status": "error",
            "error": str(e),
            "phase": "processing",
        })
        try:
            await telegram.send_error(task_id, task_content, str(e), nickname=nickname, thread_id=entry.get("thread_id"))
        except Exception:
            pass


async def main():
    config = load_config()

    if not config.vps_host:
        print("VPS_HOST is required for local worker mode.")
        print("Set it in .env or as an environment variable.")
        sys.exit(1)

    print(f"Local worker starting...")
    print(f"  VPS: {config.vps_host}")
    print(f"  Agent model: {config.agent_model}")
    print(f"  Working dir: {config.working_dir}")
    print()

    remote = RemoteState(config.vps_host)

    # Verify SSH connectivity
    test = remote.read()
    if test is None:
        print("Cannot connect to VPS via SSH. Check VPS_HOST and SSH keys.")
        sys.exit(1)
    print(f"  Connected to VPS. {len(test)} tasks in state.")
    print()

    running: dict[str, asyncio.Task] = {}

    while True:
        try:
            # Clean up finished tasks
            for tid in list(running):
                if running[tid].done():
                    exc = running[tid].exception() if not running[tid].cancelled() else None
                    if exc:
                        print(f"[!] Task {tid} raised: {exc}")
                    del running[tid]

            # Send heartbeat
            remote.heartbeat()

            # Check for pending tasks
            pending = remote.get_pending_local_tasks()
            for task_id, entry in pending.items():
                if task_id in running:
                    continue
                bg = asyncio.create_task(execute_local_task(task_id, entry, config, remote))
                running[task_id] = bg

            await asyncio.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break

    for tid, bg in running.items():
        bg.cancel()
    if running:
        await asyncio.gather(*running.values(), return_exceptions=True)

    print("\nLocal worker stopped.")


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nLocal worker stopped.")


if __name__ == "__main__":
    run()
