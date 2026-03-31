"""Main entry point for Todoist Delegator."""

import asyncio
import sys
import time
import uuid
from pathlib import Path

import httpx

from .config import Config, load_config
from .state import TaskState
from .poller import DelegatedTask, Poller
from .planner import Planner
from .router import Router
from .dispatcher import Dispatcher, DispatchResult
from .delivery import Delivery
from .telegram import TelegramBot, _escape_md
from .chatbot import Chatbot

APPROVAL_WORDS = {"go", "ok", "yes", "approve", "approved", "lgtm", "looks good", "done", "retry"}

# HTTP status codes worth retrying automatically
TRANSIENT_HTTP_CODES = {429, 529, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [10, 30, 60]

# Local worker heartbeat
LOCAL_HEARTBEAT_MAX_AGE = 120  # seconds
PENDING_LOCAL_TIMEOUT = 180  # seconds before VPS takes over


def _is_transient_error(exc: Exception) -> bool:
    """Check if an exception is a transient HTTP error worth retrying."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_HTTP_CODES
    # Agent SDK may wrap errors as RuntimeError with status code in message
    msg = str(exc)
    return any(str(code) in msg for code in TRANSIENT_HTTP_CODES)


def _parse_execution_target(content: str) -> tuple[str, str]:
    """Parse vps:/local: prefix from task content. Returns (clean_content, target)."""
    stripped = content.strip()
    lower = stripped.lower()
    if lower.startswith("vps:"):
        return stripped[4:].strip(), "vps"
    if lower.startswith("local:"):
        return stripped[6:].strip(), "local"
    return stripped, "auto"


def _is_local_online(config: Config) -> bool:
    """Check if a local worker has sent a heartbeat recently."""
    hb = Path(config.working_dir).parent / ".local-heartbeat"
    if not hb.exists():
        return False
    return (time.time() - hb.stat().st_mtime) < LOCAL_HEARTBEAT_MAX_AGE


def _get_execution_target(task_id: str, state: TaskState, config: Config) -> str:
    """Determine where to run a task: 'vps' or 'local'."""
    saved = state.get(task_id)
    target = saved.get("execution_target", "auto") if saved else "auto"
    if target == "vps":
        return "vps"
    if target == "local":
        return "local"
    # auto: prefer local if worker is online
    return "local" if _is_local_online(config) else "vps"


async def _start_execution(task_id: str, plan_context: dict, state: TaskState,
                           config: Config, human_completed: str = "") -> str:
    """Route task to VPS processing or local queue. Returns 'vps' or 'local'."""
    target = _get_execution_target(task_id, state, config)
    if target == "local":
        state.set_pending_local(task_id, plan_context, human_completed=human_completed)
    else:
        state.set_processing(task_id, plan_context, human_completed=human_completed)
    return target


async def execute_task(task, config, state, planner, router, dispatcher, delivery, telegram):
    """Execute a task that has already been planned and approved (status=processing)."""
    saved = state.get(task.task_id)
    plan_context = {
        "task": task,
        "plan": saved["plan"],
        "use_user_browser": saved.get("use_user_browser", False),
        "output_dir": saved.get("output_dir"),
    }
    human_completed = saved.get("human_completed", "")

    # --- Route ---
    print(f"    Classifying task type...")
    state.set_processing(task.task_id, plan_context, task_content=task.content)
    routed = await router.route(plan_context)

    if human_completed:
        routed.agent_prompt += (
            f"\n\n## Resume After Human Action\n"
            f"A previous agent run requested human help: \"{human_completed}\"\n"
            f"The human has confirmed they completed this action. "
            f"Continue from where the previous run left off — do NOT repeat the request for human help. "
            f"The action has already been done."
        )

    print(f"    Type: {routed.task_type}")

    # --- Dispatch ---
    print("    Dispatching to agent...")
    result = await dispatcher.dispatch(task.task_id, routed)
    print(f"    Agent finished: success={result.success}, cost=${result.cost_usd:.4f}")

    # --- Check if agent needs human help ---
    if result.needs_human:
        print(f"    Agent needs human help: {result.needs_human}")
        nickname = state.get_nickname(task.task_id)
        tid = state.get_thread_id(task.task_id)
        await telegram.send_needs_human(task.task_id, task.content, result.needs_human, nickname=nickname, thread_id=tid)
        state.set_waiting_for_human(task.task_id, result.needs_human, plan_context)
        return

    # --- Send for review ---
    print("    Sending results for review...")
    await delivery.send_for_review(task.task_id, task.content, result, plan_context)
    print(f"[+] Awaiting review: {task.content}")


async def handle_new_task(task, state, planner, telegram):
    """Generate a plan for a new task and send it to Telegram for approval."""
    # Parse execution target prefix (vps:/local:) from task content
    clean_content, exec_target = _parse_execution_target(task.content)
    if exec_target != "auto":
        task.content = clean_content  # strip prefix for display/planning
    print(f"[+] New task: {task.content} (id={task.task_id}, target={exec_target})")
    state.set_planning(task.task_id, task_content=task.content)
    state._data[task.task_id]["execution_target"] = exec_target
    state._save()
    nickname = state.assign_nickname(task.task_id, task.content)
    print(f"    Nickname: {nickname}")

    # Create a forum topic for this task
    thread_id = await telegram.create_topic(f"{nickname}: {task.content[:80]}")
    if thread_id:
        state._data[task.task_id]["thread_id"] = thread_id
        state._save()

    print("    Generating plan...")
    plan_text = await planner.generate_plan(task)
    planner.save_plan(task.task_id, plan_text)
    await telegram.send_plan(task.task_id, task.content, plan_text, nickname=nickname, thread_id=thread_id)
    state.set_awaiting_approval(task.task_id, plan_text, task_content=task.content)
    print("    Plan sent to Telegram, awaiting approval.")


async def _handle_status_command(state: TaskState, telegram: TelegramBot, config: Config) -> None:
    """Report on all tasks currently in processing state."""
    processing = state.get_processing_tasks()

    if not processing:
        active = {
            tid: entry for tid, entry in state._data.items()
            if entry.get("status") not in {"completed", "failed"}
        }
        if active:
            lines = ["No tasks currently processing. Active tasks:"]
            for tid, entry in active.items():
                nickname = state.get_nickname(tid)
                lines.append(f"- *{nickname}*: {entry.get('status', 'unknown')}")
            await telegram.send_message("\n".join(lines))
        else:
            await telegram.send_message("No active tasks.")
        return

    lines = []
    for task_id, entry in processing.items():
        started = entry.get("processing_started_at")
        if started:
            elapsed = time.time() - started
            minutes, seconds = divmod(int(elapsed), 60)
            elapsed_str = f"{minutes}m {seconds}s"
        else:
            elapsed_str = "unknown"

        # Scan task directory for files
        task_dir = Path(config.working_dir) / f"task-{task_id}"
        files = []
        if task_dir.exists():
            files = [
                str(f.relative_to(task_dir))
                for f in task_dir.rglob("*")
                if f.is_file() and f.name != "plan.md"
            ]

        task_content = entry.get("task_content", "(unknown task)")
        nickname = state.get_nickname(task_id)
        plan_preview = entry.get("plan", "")[:100]

        lines.append(f"*Processing:* {_escape_md(task_content)}")
        lines.append(f"Name: *{nickname}*")
        lines.append(f"Elapsed: {elapsed_str}")
        if plan_preview:
            lines.append(f"Plan: {_escape_md(plan_preview)}...")
        lines.append(f"Files created: {len(files)}")
        if files:
            for f_name in files[:10]:
                lines.append(f"  - `{f_name}`")
            if len(files) > 10:
                lines.append(f"  ... and {len(files) - 10} more")
        lines.append("")

    await telegram.send_message("\n".join(lines))


def _parse_reply(reply: str) -> tuple[str, bool, str | None]:
    """Parse a Telegram reply into (base_word, is_approval, target_name).

    Examples:
        "go"          -> ("go", True, None)
        "go slides"   -> ("go", True, "slides")
        "hello world" -> ("hello world", False, None)
    """
    content = reply.strip().lower()
    parts = content.split(maxsplit=1)
    base_word = parts[0] if parts else content
    if base_word in APPROVAL_WORDS and len(parts) == 2:
        return base_word, True, parts[1].strip()
    is_approval = content in APPROVAL_WORDS
    return content, is_approval, None


def _extract_nickname_prefix(reply: str, state: TaskState) -> tuple[str, str | None]:
    """Check if message starts with 'nickname: ...' targeting a specific task.

    Returns (remaining_message, task_id) or (original_reply, None).
    """
    # Look for "nickname: message" or "nickname message" pattern at start
    stripped = reply.strip()
    # Try colon separator first: "go-benheimart: tell me more"
    if ":" in stripped:
        prefix, rest = stripped.split(":", 1)
        prefix = prefix.strip().lower()
        # Don't match reserved prefixes
        if prefix in ("new", "vps", "local", "status"):
            return reply, None
        # Check if prefix matches an active task nickname
        active_ids = [
            tid for tid, entry in state._data.items()
            if entry.get("status") not in {"completed", "failed"}
        ]
        matched = state.find_by_nickname(prefix, active_ids)
        if matched:
            return rest.strip() or reply, matched[0]
    return reply, None


def _filter_by_target(task_ids: list[str], target: str | None, state) -> list[str]:
    """Filter task IDs by nickname match (exact then prefix)."""
    if not target:
        return task_ids
    return state.find_by_nickname(target, task_ids)


async def _disambiguate(state, telegram, task_ids: list[str], action: str) -> None:
    """Ask user to specify which task when multiple match."""
    lines = [f"Multiple tasks {action}. Which one?"]
    for tid in task_ids:
        nickname = state.get_nickname(tid)
        saved = state.get(tid)
        name = saved.get("task_content", "")
        lines.append(f"- *{nickname}*: {name}" if name else f"- *{nickname}*")
    first_nick = state.get_nickname(task_ids[0])
    lines.append(f'\nPrefix with the name: `{first_nick}: your message`')
    lines.append(f'Or to approve: `done {first_nick}`')
    await telegram.send_message("\n".join(lines))


async def _handle_targeted_message(reply, task_id, state, telegram, delivery, planner, poller, chatbot, config):
    """Handle a message explicitly targeted at a specific task (via thread or nickname)."""
    saved = state.get(task_id)
    if not saved:
        await telegram.send_message("Task not found.")
        return
    status = saved.get("status")
    content = reply.strip().lower()
    is_approval = content in APPROVAL_WORDS
    nickname = state.get_nickname(task_id)
    tid = saved.get("thread_id")  # forum thread for responses

    if status == "error" and is_approval:
        phase = saved.get("phase", "planning")
        if phase == "processing" and saved.get("plan"):
            plan_context = {
                "plan": saved["plan"],
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            target = await _start_execution(task_id, plan_context, state, config)
            print(f"[+] Retrying errored task {task_id} (processing, target={target})")
        elif saved.get("source") == "telegram":
            task_content = saved.get("task_content", "")
            task = DelegatedTask(
                task_id=task_id, content=task_content, description="",
                project_name="", labels=[], comments=[],
            )
            state.set_planning(task_id, task_content=task_content)
            state._data[task_id]["source"] = "telegram"
            state._save()
            await handle_new_task(task, state, planner, telegram)
        else:
            del state._data[task_id]
            state._save()
        await telegram.send_message("Got it — retrying now.", thread_id=tid)
        return

    if status == "conversing":
        if is_approval:
            from_status = saved.get("from_status", "awaiting_approval")
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            history = saved.get("conversation_history", [])
            if history:
                lines = []
                for m in history:
                    label = "User" if m["role"] == "user" else "Assistant"
                    c = m["content"]
                    if isinstance(c, list):
                        text = " ".join(
                            b["text"] for b in c
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                        )
                    else:
                        text = c
                    if text:
                        lines.append(f"{label}: {text}")
                plan_context["plan"] += f"\n\n## Refined requirements from conversation\n" + "\n".join(lines)
            target = await _start_execution(task_id, plan_context, state, config)
            if target == "local":
                await telegram.send_message(f"Got it — queued for local execution.", thread_id=tid)
            else:
                await telegram.send_message(f"Got it — launching now.", thread_id=tid)
        else:
            state.append_conversation(task_id, "user", reply)
            try:
                display_text, raw_content = await chatbot.chat(
                    task_content=saved.get("task_content", ""),
                    plan=saved.get("plan", ""),
                    conversation_history=saved.get("conversation_history", []),
                    from_status=saved.get("from_status", ""),
                    result_summary=saved.get("result_summary", ""),
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text, thread_id=tid)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}\nKeep chatting or say \"go\" to proceed.", thread_id=tid)
        return

    if status == "awaiting_approval":
        if is_approval:
            print(f"[+] Plan approved for task {task_id}")
            tasks = poller.poll_by_id(task_id)
            if tasks:
                task = tasks[0]
                plan_context = {
                    "plan": saved["plan"],
                    "use_user_browser": planner.looks_like_browser_task(task),
                    "output_dir": planner.extract_output_dir(task),
                }
            else:
                plan_context = {
                    "plan": saved["plan"],
                    "use_user_browser": False,
                    "output_dir": None,
                }
            target = await _start_execution(task_id, plan_context, state, config)
            if target == "local":
                await telegram.send_message(f"Approved — queued for local execution.", thread_id=tid)
            else:
                await telegram.send_message(f"Approved — launching agent now.", thread_id=tid)
        else:
            # Feedback — enter conversation
            task_content = saved.get("task_content", "")
            if not task_content:
                tasks = poller.poll_by_id(task_id)
                task_content = tasks[0].content if tasks else ""
            plan_context = {"plan": saved.get("plan", ""), "use_user_browser": False, "output_dir": None}
            state.set_conversing(task_id, "awaiting_approval", plan_context, task_content)
            state.append_conversation(task_id, "user", reply)
            try:
                display_text, raw_content = await chatbot.chat(
                    task_content=task_content,
                    plan=saved.get("plan", ""),
                    conversation_history=[{"role": "user", "content": reply}],
                    from_status="awaiting_approval",
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text, thread_id=tid)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}", thread_id=tid)
        return

    if status == "waiting_for_human" and is_approval:
        human_completed = saved.get("waiting_message", "")
        plan_context = {
            "plan": saved.get("plan", ""),
            "use_user_browser": saved.get("use_user_browser", False),
            "output_dir": saved.get("output_dir"),
        }
        await _start_execution(task_id, plan_context, state, config, human_completed=human_completed)
        await telegram.send_message("Resuming agent.", thread_id=tid)
        return

    if status == "awaiting_review":
        if is_approval:
            print(f"[+] Human approved result for task {task_id} — completing")
            delivery.complete(task_id)
            await telegram.send_message("Marked complete.", thread_id=tid)
            # Delete the topic after completion
            if tid:
                await telegram.delete_topic(tid)
        else:
            # Enter conversation about results
            task_content = saved.get("task_content", "")
            if not task_content:
                tasks = poller.poll_by_id(task_id)
                task_content = tasks[0].content if tasks else ""
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            state.set_conversing(task_id, "awaiting_review", plan_context, task_content)
            state.append_conversation(task_id, "user", reply)
            try:
                display_text, raw_content = await chatbot.chat(
                    task_content=task_content,
                    plan=saved.get("plan", ""),
                    conversation_history=[{"role": "user", "content": reply}],
                    from_status="awaiting_review",
                    result_summary=saved.get("result_summary", ""),
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text, thread_id=tid)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}", thread_id=tid)
        return

    await telegram.send_message(f"Task is in `{status}` state — not sure what to do with that message.", thread_id=tid)


async def handle_telegram_reply(reply, thread_id, state, telegram, delivery, planner, poller, chatbot, config):
    """Route a single Telegram reply to the right task based on thread or content."""
    content = reply.strip().lower()

    # Forum mode: if message is in a task's thread, route directly to that task
    if thread_id:
        task_id = state.find_by_thread(thread_id)
        if task_id:
            await _handle_targeted_message(reply, task_id, state, telegram, delivery, planner, poller, chatbot, config)
            return
        # Message in an unknown thread (maybe General) — fall through to global handling

    # Global commands — handled before task-state routing
    if content == "status":
        await _handle_status_command(state, telegram, config)
        return

    # Check for nickname-targeted message: "nickname: message"
    remaining, targeted_task_id = _extract_nickname_prefix(reply, state)
    if targeted_task_id:
        await _handle_targeted_message(remaining, targeted_task_id, state, telegram, delivery, planner, poller, chatbot, config)
        return

    # Direct task creation via "new:" prefix — bypasses Todoist
    if content.startswith("new:"):
        task_content = reply.strip()[4:].strip()  # preserve original case
        if not task_content:
            await telegram.send_message("Usage: `new: <task description>`")
            return
        task_id = f"tg-{uuid.uuid4().hex[:12]}"
        task = DelegatedTask(
            task_id=task_id,
            content=task_content,
            description="",
            project_name="",
            labels=[],
            comments=[],
        )
        # Mark as telegram-sourced so we skip Todoist on completion
        state.set_planning(task_id, task_content=task_content)
        state._data[task_id]["source"] = "telegram"
        state._save()
        await handle_new_task(task, state, planner, telegram)
        return

    _base_word, is_approval, target_name = _parse_reply(reply)

    # Priority 0: error tasks — user can retry
    error_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "error"
    ]
    if error_ids and (is_approval or content == "retry"):
        matched = _filter_by_target(error_ids, target_name, state)
        if len(matched) > 1:
            await _disambiguate(state, telegram, matched, "in error state")
            return
        if not matched:
            await telegram.send_message(f'No error task matching "{target_name}". Check the name and try again.')
            return
        for task_id in matched:
            saved = state.get(task_id)
            phase = saved.get("phase", "planning")
            if phase == "processing" and saved.get("plan"):
                plan_context = {
                    "plan": saved["plan"],
                    "use_user_browser": saved.get("use_user_browser", False),
                    "output_dir": saved.get("output_dir"),
                }
                target = await _start_execution(task_id, plan_context, state, config)
                print(f"[+] Retrying errored task {task_id} (processing phase, target={target})")
            elif saved.get("source") == "telegram":
                # Telegram task — re-plan inline (poller won't find it)
                task_content = saved.get("task_content", "")
                task = DelegatedTask(
                    task_id=task_id, content=task_content, description="",
                    project_name="", labels=[], comments=[],
                )
                state.set_planning(task_id, task_content=task_content)
                state._data[task_id]["source"] = "telegram"
                state._save()
                await handle_new_task(task, state, planner, telegram)
                print(f"[+] Retrying errored telegram task {task_id} (planning phase)")
            else:
                # Reset to new so the poller picks it up fresh
                del state._data[task_id]
                state._save()
                print(f"[+] Retrying errored task {task_id} (planning phase)")
        await telegram.send_message("Got it — retrying now.")
        return

    # Priority 1: conversing tasks (ongoing LLM chat)
    convo_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "conversing"
    ]
    if convo_ids:
        matched = _filter_by_target(convo_ids, target_name, state)
        if len(matched) > 1:
            await _disambiguate(state, telegram, matched, "in conversation")
            return
        if not matched:
            if target_name:
                await telegram.send_message(f'No conversing task matching "{target_name}". Check the name and try again.')
                return
            matched = convo_ids  # non-approval message with no target — fall through to first
        task_id = matched[0]
        saved = state.get(task_id)
        if is_approval:
            # User says "go" — extract refined context and transition
            from_status = saved.get("from_status", "awaiting_approval")
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            # Summarize conversation into additional context for the plan
            history = saved.get("conversation_history", [])
            if history:
                lines = []
                for m in history:
                    label = "User" if m["role"] == "user" else "Assistant"
                    content = m["content"]
                    # Assistant content may be a list of blocks (from web_search turns)
                    if isinstance(content, list):
                        text = " ".join(
                            b["text"] for b in content
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                        )
                    else:
                        text = content
                    if text:
                        lines.append(f"{label}: {text}")
                convo_summary = "\n".join(lines)
                plan_context["plan"] += f"\n\n## Refined requirements from conversation\n{convo_summary}"

            if from_status == "awaiting_review":
                print(f"[+] Conversation done for task {task_id} — re-running agent")
                target = await _start_execution(task_id, plan_context, state, config)
                if target == "local":
                    await telegram.send_message("Got it — queued for local execution.")
                else:
                    await telegram.send_message("Got it — re-running the agent with your refined requirements.")
            else:
                print(f"[+] Conversation done for task {task_id} — approved, moving to processing")
                target = await _start_execution(task_id, plan_context, state, config)
                if target == "local":
                    await telegram.send_message("Got it — queued for local execution.")
                else:
                    await telegram.send_message("Got it — launching the agent now.")
        else:
            # Continue the conversation with the LLM
            print(f"[+] Chatting about task {task_id}: {reply[:80]}")
            state.append_conversation(task_id, "user", reply)
            try:
                display_text, raw_content = await chatbot.chat(
                    task_content=saved.get("task_content", ""),
                    plan=saved.get("plan", ""),
                    conversation_history=saved.get("conversation_history", []),
                    from_status=saved.get("from_status", ""),
                    result_summary=saved.get("result_summary", ""),
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}\nYou can keep chatting or say \"go\" to proceed.")
        return

    # Priority 2: awaiting_approval tasks
    approval_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "awaiting_approval"
    ]
    if approval_ids:
        matched = _filter_by_target(approval_ids, target_name, state)
        if is_approval:
            if len(matched) > 1:
                await _disambiguate(state, telegram, matched, "awaiting approval")
                return
            if not matched:
                if target_name:
                    await telegram.send_message(f'No approval task matching "{target_name}". Check the name and try again.')
                else:
                    await _disambiguate(state, telegram, approval_ids, "awaiting approval")
                return
            task_id = matched[0]
            saved = state.get(task_id)
            print(f"[+] Plan approved for task {task_id}")
            # Fetch task object to check browser/output dir
            tasks = poller.poll_by_id(task_id)
            if tasks:
                task = tasks[0]
                plan_context = {
                    "plan": saved["plan"],
                    "use_user_browser": planner.looks_like_browser_task(task),
                    "output_dir": planner.extract_output_dir(task),
                }
            else:
                plan_context = {
                    "plan": saved["plan"],
                    "use_user_browser": False,
                    "output_dir": None,
                }
            target = await _start_execution(task_id, plan_context, state, config)
            nickname = state.get_nickname(task_id)
            if target == "local":
                await telegram.send_message(f"Approved *{nickname}* — queued for local execution.")
            else:
                await telegram.send_message(f"Approved *{nickname}* — launching agent now.")
        else:
            # Feedback — enter conversation mode instead of immediately regenerating
            if len(matched) > 1:
                await _disambiguate(state, telegram, matched, "awaiting approval")
                return
            task_id = matched[0] if matched else approval_ids[0]
            saved = state.get(task_id)
            print(f"[+] Starting conversation about plan for task {task_id}")
            tasks = poller.poll_by_id(task_id)
            task_content = tasks[0].content if tasks else ""
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": False,
                "output_dir": None,
            }
            state.set_conversing(task_id, "awaiting_approval", plan_context, task_content)
            state.append_conversation(task_id, "user", reply)
            try:
                display_text, raw_content = await chatbot.chat(
                    task_content=task_content,
                    plan=saved.get("plan", ""),
                    conversation_history=[{"role": "user", "content": reply}],
                    from_status="awaiting_approval",
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}\nYou can keep chatting or say \"go\" to proceed.")
        return

    # Priority 3: waiting_for_human tasks
    waiting_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "waiting_for_human"
    ]
    if waiting_ids and is_approval:
        matched = _filter_by_target(waiting_ids, target_name, state)
        if len(matched) > 1:
            await _disambiguate(state, telegram, matched, "waiting for human action")
            return
        if not matched:
            if target_name:
                await telegram.send_message(f'No waiting task matching "{target_name}". Check the name and try again.')
                return
            matched = waiting_ids
        for task_id in matched:
            print(f"[+] Human confirmed done for task {task_id} — resuming")
            saved = state.get(task_id)
            human_completed = saved.get("waiting_message", "")
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            await _start_execution(task_id, plan_context, state, config, human_completed=human_completed)
        return

    # Priority 4: awaiting_review tasks
    review_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "awaiting_review"
    ]
    if review_ids:
        matched = _filter_by_target(review_ids, target_name, state)
        if is_approval:
            if len(matched) > 1:
                await _disambiguate(state, telegram, matched, "awaiting review")
                return
            if not matched:
                if target_name:
                    await telegram.send_message(f'No review task matching "{target_name}". Check the name and try again.')
                else:
                    await _disambiguate(state, telegram, review_ids, "awaiting review")
                return
            for task_id in matched:
                nickname = state.get_nickname(task_id)
                print(f"[+] Human approved result for task {task_id} — completing")
                delivery.complete(task_id)
                await telegram.send_message(f"*{nickname}* marked complete.")
        else:
            # Enter conversation mode to refine requirements before re-running
            if len(matched) > 1:
                await _disambiguate(state, telegram, matched, "awaiting review")
                return
            task_id = matched[0] if matched else review_ids[0]
            saved = state.get(task_id)
            print(f"[+] Starting conversation about results for task {task_id}")
            task_content = saved.get("task_content", "")
            # Try to get task content from Todoist if not in state
            if not task_content:
                tasks = poller.poll_by_id(task_id)
                task_content = tasks[0].content if tasks else ""
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            state.set_conversing(task_id, "awaiting_review", plan_context, task_content)
            state.append_conversation(task_id, "user", reply)
            try:
                display_text, raw_content = await chatbot.chat(
                    task_content=task_content,
                    plan=saved.get("plan", ""),
                    conversation_history=[{"role": "user", "content": reply}],
                    from_status="awaiting_review",
                    result_summary=saved.get("result_summary", ""),
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}\nYou can keep chatting or say \"go\" to proceed.")


async def main():
    config = load_config()

    missing = config.validate()
    if missing:
        print(f"Missing required config: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the values.")
        sys.exit(1)

    state_file = Path(config.working_dir) / "state.json"
    state = TaskState(state_file)

    # If VPS_HOST is set, this instance also acts as a local worker
    remote = None
    if config.vps_host:
        from .local_worker import RemoteState
        remote = RemoteState(config.vps_host)
        test = remote.read()
        print(f"Local worker mode: connected to VPS {config.vps_host} ({len(test)} tasks in state)")

    print("Todoist Delegator starting...")
    print(f"  Label: @{config.delegate_label_name}")
    print(f"  Poll interval: {config.poll_interval_seconds}s")
    print(f"  Agent model: {config.agent_model}")
    print(f"  State file: {state_file}")
    print()

    telegram = TelegramBot(config)
    poller = Poller(config, state)
    planner = Planner(config)
    router = Router(config)
    dispatcher = Dispatcher(config, telegram)
    delivery = Delivery(poller, state, telegram)
    chatbot = Chatbot(config)

    # Track background task executions so we can poll Telegram while agents run
    running_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task

    async def _run_task_with_retry(task, task_status):
        """Execute a task with transient-error retry logic."""
        try:
            if task_status == "processing":
                await execute_task(task, config, state, planner, router, dispatcher, delivery, telegram)
            elif task_status is None:
                await handle_new_task(task, state, planner, telegram)
        except Exception as e:
            if _is_transient_error(e):
                for attempt, delay in enumerate(RETRY_BACKOFF_SECONDS, 1):
                    print(f"[!] Transient error on task {task.task_id} (attempt {attempt}/{MAX_RETRIES}): {e}")
                    print(f"    Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    try:
                        if task_status == "processing":
                            await execute_task(task, config, state, planner, router, dispatcher, delivery, telegram)
                        else:
                            await handle_new_task(task, state, planner, telegram)
                        return  # retry succeeded
                    except Exception as retry_e:
                        if not _is_transient_error(retry_e):
                            e = retry_e
                            break
                        e = retry_e

            # All retries exhausted or non-transient error
            print(f"[!] Error processing task {task.task_id}: {e}")
            phase = "processing" if task_status == "processing" else "planning"
            saved = state.get(task.task_id)
            plan_context = None
            if saved and saved.get("plan"):
                plan_context = {
                    "plan": saved.get("plan", ""),
                    "use_user_browser": saved.get("use_user_browser", False),
                    "output_dir": saved.get("output_dir"),
                }
            state.set_error(task.task_id, str(e), phase, plan_context)
            try:
                nickname = state.get_nickname(task.task_id)
                tid = state.get_thread_id(task.task_id)
                await telegram.send_error(task.task_id, task.content, str(e), nickname=nickname, thread_id=tid)
            except Exception:
                pass

    while True:
        try:
            # Clean up finished background tasks
            for tid in list(running_tasks):
                if running_tasks[tid].done():
                    # Surface any unexpected exceptions
                    exc = running_tasks[tid].exception() if not running_tasks[tid].cancelled() else None
                    if exc:
                        print(f"[!] Background task {tid} raised: {exc}")
                    del running_tasks[tid]

            # When VPS_HOST is set, VPS handles Telegram and Todoist — we only do worker duties
            if not remote:
                # Single place to check Telegram replies — routes to the right handler
                result = await telegram.poll_for_reply(timeout=2.0)
                if result:
                    reply_text, reply_thread_id = result
                    await handle_telegram_reply(reply_text, reply_thread_id, state, telegram, delivery, planner, poller, chatbot, config)

            # Poll for new/resumable tasks (skip when VPS handles ingestion)
            if not remote:
                tasks = poller.poll()
            else:
                tasks = []
            if tasks:
                print(f"Found {len(tasks)} task(s) to process")
            for task in tasks:
                # Skip if already running in background
                if task.task_id in running_tasks:
                    continue
                task_status = state.status(task.task_id)
                if task_status == "processing":
                    # Run agent dispatch in background so we keep polling Telegram
                    bg = asyncio.create_task(_run_task_with_retry(task, task_status))
                    running_tasks[task.task_id] = bg
                    print(f"    Launched background task for {task.task_id}")
                elif task_status is None:
                    # Planning is quick — run inline
                    try:
                        await handle_new_task(task, state, planner, telegram)
                    except Exception as e:
                        print(f"[!] Error planning task {task.task_id}: {e}")
                        state.set_error(task.task_id, str(e), "planning")
                        try:
                            nickname = state.get_nickname(task.task_id)
                            tid = state.get_thread_id(task.task_id)
                            await telegram.send_error(task.task_id, task.content, str(e), nickname=nickname, thread_id=tid)
                        except Exception:
                            pass

            # VPS-only: pick up telegram-sourced and handle pending_local timeouts
            if not remote:
                # Pick up telegram-sourced tasks in processing state (not in Todoist)
                for task_id, entry in state.get_telegram_processing_tasks().items():
                    if task_id in running_tasks:
                        continue
                    task = DelegatedTask(
                        task_id=task_id,
                        content=entry.get("task_content", ""),
                        description="",
                        project_name="",
                        labels=[],
                        comments=[],
                    )
                    bg = asyncio.create_task(_run_task_with_retry(task, "processing"))
                    running_tasks[task_id] = bg
                    print(f"    Launched background task for telegram task {task_id}")

                # Check for pending_local tasks that timed out — VPS takes over
                for task_id, entry in state.get_pending_local_tasks().items():
                    pending_since = entry.get("pending_local_since", 0)
                    if time.time() - pending_since > PENDING_LOCAL_TIMEOUT:
                        nickname = state.get_nickname(task_id)
                        print(f"[+] Local timeout for {task_id} ({nickname}), running on VPS")
                        plan_context = {
                            "plan": entry.get("plan", ""),
                            "use_user_browser": entry.get("use_user_browser", False),
                            "output_dir": entry.get("output_dir"),
                        }
                        state.set_processing(task_id, plan_context,
                                             human_completed=entry.get("human_completed", ""))
                        await telegram.send_message(
                            f"Local didn't pick up *{nickname}* — running on VPS."
                        )

            # Local worker mode: poll VPS for pending_local tasks and execute here
            if remote:
                remote.heartbeat()
                for task_id, entry in remote.get_pending_local_tasks().items():
                    if task_id in running_tasks:
                        continue
                    from .local_worker import execute_local_task
                    bg = asyncio.create_task(
                        execute_local_task(task_id, entry, config, remote)
                    )
                    running_tasks[task_id] = bg
                    nickname = entry.get("nickname", task_id[:8])
                    print(f"    Picked up VPS task {task_id} ({nickname}) for local execution")

            await asyncio.sleep(config.poll_interval_seconds)

        except KeyboardInterrupt:
            break

    # Cancel any running background tasks on shutdown
    for tid, bg_task in running_tasks.items():
        bg_task.cancel()
    if running_tasks:
        await asyncio.gather(*running_tasks.values(), return_exceptions=True)

    print("\nShutting down.")


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    run()
