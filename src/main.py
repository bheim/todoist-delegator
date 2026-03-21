"""Main entry point for Todoist Delegator."""

import asyncio
import sys
from pathlib import Path

from todoist_api_python.api import TodoistAPI

from .config import load_config
from .state import TaskState
from .poller import Poller
from .clarifier import Clarifier
from .router import Router
from .dispatcher import Dispatcher, DispatchResult
from .delivery import Delivery


async def process_task(task, state, clarifier, router, dispatcher, delivery):
    """Process a single delegated task, resuming at the right stage."""
    task_status = state.status(task.task_id)

    # --- Stage 1: Clarification ---
    if task_status == "processing":
        # Already clarified — restore saved clarification data
        saved = state.get(task.task_id)
        clarification = saved["clarification"]
        clarification["task"] = task  # re-attach live task object
        print(f"[+] Resuming task: {task.content} (id={task.task_id}) — skipping clarification")
    elif task_status == "clarifying":
        # Was mid-clarification — check if answers already exist in comments
        if clarifier.has_existing_clarification(task):
            print(f"[+] Resuming task: {task.content} (id={task.task_id}) — parsing existing Q&A")
            clarification = clarifier.parse_existing_clarification(task)
        else:
            print(f"[+] Re-clarifying task: {task.content} (id={task.task_id})")
            state.set_clarifying(task.task_id)
            clarification = await clarifier.clarify(task)
    else:
        # New task (or first run with no state file)
        if clarifier.has_existing_clarification(task):
            print(f"[+] Found existing Q&A for: {task.content} (id={task.task_id})")
            clarification = clarifier.parse_existing_clarification(task)
        else:
            print(f"[+] New task: {task.content} (id={task.task_id})")
            state.set_clarifying(task.task_id)
            print("    Clarifying...")
            clarification = await clarifier.clarify(task)

    # --- Stage 2: Route ---
    print("    Classifying task type...")
    state.set_processing(task.task_id, clarification)
    routed = await router.route(clarification)
    print(f"    Type: {routed.task_type}")

    # --- Stage 3: Dispatch ---
    print("    Dispatching to agent...")
    result = await dispatcher.dispatch(task.task_id, routed)
    print(f"    Agent finished: success={result.success}, cost=${result.cost_usd:.4f}")

    # --- Stage 3b: Check if agent needs human help ---
    if result.needs_human:
        print(f"    Agent needs human help: {result.needs_human}")
        saved = state.get(task.task_id)
        clarification_data = saved.get("clarification") if saved else None
        state.set_waiting_for_human(task.task_id, result.needs_human, clarification_data)
        # Post comment to Todoist telling the user what to do
        delivery.poller.add_comment(
            task.task_id,
            f"🖐️ **Action needed:** {result.needs_human}\n\n"
            f"When you're done, reply `done` here and I'll continue.",
        )
        return

    # --- Stage 4: Deliver ---
    print("    Delivering results...")
    delivery.deliver(task.task_id, task.content, result)
    print(f"[+] Done: {task.content}")


def check_waiting_tasks(state: TaskState, todoist: TodoistAPI) -> list[str]:
    """Check waiting_for_human tasks for a 'done' reply. Returns task IDs ready to resume."""
    ready = []
    for task_id, entry in state._data.items():
        if entry.get("status") != "waiting_for_human":
            continue
        # Check recent comments for "done"
        try:
            comments = []
            for page in todoist.get_comments(task_id=task_id):
                comments.extend(page)
            # Look at the last few comments for a "done" reply
            for c in comments[-5:]:
                content = c.content.strip().lower()
                if content == "done" or content.startswith("done"):
                    ready.append(task_id)
                    break
        except Exception:
            pass
    return ready


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

    poller = Poller(config, state)
    clarifier = Clarifier(config)
    router = Router(config)
    dispatcher = Dispatcher(config)
    delivery = Delivery(poller, state)
    todoist = TodoistAPI(config.todoist_api_token)

    while True:
        try:
            # Check if any waiting tasks got a "done" reply
            ready_ids = check_waiting_tasks(state, todoist)
            for task_id in ready_ids:
                print(f"[+] Human confirmed done for task {task_id} — resuming")
                # Move back to processing so the poller picks it up
                saved = state.get(task_id)
                clarification = saved.get("clarification")
                if clarification:
                    state.set_processing(task_id, clarification)
                else:
                    state.set_clarifying(task_id)

            # Poll for new/resumable tasks
            tasks = poller.poll()
            if tasks:
                print(f"Found {len(tasks)} task(s) to process")
            for task in tasks:
                try:
                    await process_task(task, state, clarifier, router, dispatcher, delivery)
                except Exception as e:
                    print(f"[!] Error processing task {task.task_id}: {e}")
                    state.set_failed(task.task_id, str(e))
                    try:
                        delivery.deliver(
                            task.task_id,
                            task.content,
                            DispatchResult(
                                success=False,
                                summary=f"Error: {e}",
                                output_files=[],
                                cost_usd=0.0,
                            ),
                        )
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
