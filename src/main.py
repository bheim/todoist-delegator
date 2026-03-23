"""Main entry point for Todoist Delegator."""

import asyncio
import sys
from pathlib import Path

from .config import load_config
from .state import TaskState
from .poller import Poller
from .planner import Planner
from .router import Router
from .dispatcher import Dispatcher, DispatchResult
from .delivery import Delivery
from .telegram import TelegramBot
from .chatbot import Chatbot

APPROVAL_WORDS = {"go", "ok", "yes", "approve", "approved", "lgtm", "looks good", "done"}


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
    state.set_processing(task.task_id, plan_context)
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
        await telegram.send_needs_human(task.task_id, task.content, result.needs_human)
        state.set_waiting_for_human(task.task_id, result.needs_human, plan_context)
        return

    # --- Send for review ---
    print("    Sending results for review...")
    await delivery.send_for_review(task.task_id, task.content, result, plan_context)
    print(f"[+] Awaiting review: {task.content}")


async def handle_new_task(task, state, planner, telegram):
    """Generate a plan for a new task and send it to Telegram for approval."""
    print(f"[+] New task: {task.content} (id={task.task_id})")
    state.set_planning(task.task_id)
    print("    Generating plan...")
    plan_text = await planner.generate_plan(task)
    planner.save_plan(task.task_id, plan_text)
    await telegram.send_plan(task.task_id, task.content, plan_text)
    state.set_awaiting_approval(task.task_id, plan_text)
    print("    Plan sent to Telegram, awaiting approval.")


async def handle_telegram_reply(reply, state, telegram, delivery, planner, poller, chatbot):
    """Route a single Telegram reply to the right task based on current states."""
    content = reply.strip().lower()
    is_approval = content in APPROVAL_WORDS

    # Priority 0: conversing tasks (ongoing LLM chat)
    convo_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "conversing"
    ]
    if convo_ids:
        task_id = convo_ids[0]
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
                state.set_processing(task_id, plan_context)
                await telegram.send_message("Got it — re-running the agent with your refined requirements.")
            else:
                print(f"[+] Conversation done for task {task_id} — approved, moving to processing")
                state.set_processing(task_id, plan_context)
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
                )
                state.append_conversation(task_id, "assistant", raw_content)
                await telegram.send_message(display_text)
            except Exception as e:
                await telegram.send_message(f"Chat error: {e}\nYou can keep chatting or say \"go\" to proceed.")
        return

    # Priority 1: awaiting_approval tasks
    approval_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "awaiting_approval"
    ]
    if approval_ids:
        task_id = approval_ids[0]
        saved = state.get(task_id)
        if is_approval:
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
            state.set_processing(task_id, plan_context)
        else:
            # Feedback — enter conversation mode instead of immediately regenerating
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

    # Priority 2: waiting_for_human tasks
    waiting_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "waiting_for_human"
    ]
    if waiting_ids and is_approval:
        for task_id in waiting_ids:
            print(f"[+] Human confirmed done for task {task_id} — resuming")
            saved = state.get(task_id)
            human_completed = saved.get("waiting_message", "")
            plan_context = {
                "plan": saved.get("plan", ""),
                "use_user_browser": saved.get("use_user_browser", False),
                "output_dir": saved.get("output_dir"),
            }
            state.set_processing(task_id, plan_context, human_completed=human_completed)
        return

    # Priority 3: awaiting_review tasks
    review_ids = [
        task_id for task_id, entry in state._data.items()
        if entry.get("status") == "awaiting_review"
    ]
    if review_ids:
        if is_approval:
            for task_id in review_ids:
                print(f"[+] Human approved result for task {task_id} — completing")
                delivery.complete(task_id)
                await telegram.send_message(f"Task `{task_id}` marked complete.")
        else:
            # Enter conversation mode to refine requirements before re-running
            for task_id in review_ids:
                print(f"[+] Starting conversation about results for task {task_id}")
                saved = state.get(task_id)
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

    while True:
        try:
            # Single place to check Telegram replies — routes to the right handler
            reply = await telegram.poll_for_reply(timeout=2.0)
            if reply:
                await handle_telegram_reply(reply, state, telegram, delivery, planner, poller, chatbot)

            # Poll for new/resumable tasks
            tasks = poller.poll()
            if tasks:
                print(f"Found {len(tasks)} task(s) to process")
            for task in tasks:
                task_status = state.status(task.task_id)
                try:
                    if task_status == "processing":
                        await execute_task(task, config, state, planner, router, dispatcher, delivery, telegram)
                    elif task_status is None:
                        await handle_new_task(task, state, planner, telegram)
                except Exception as e:
                    print(f"[!] Error processing task {task.task_id}: {e}")
                    state.set_failed(task.task_id, str(e))
                    try:
                        await telegram.send_error(task.task_id, task.content, str(e))
                    except Exception:
                        pass

            await asyncio.sleep(config.poll_interval_seconds)

        except KeyboardInterrupt:
            break

    print("\nShutting down.")


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    run()
