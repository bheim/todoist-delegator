# Todoist Delegator

## What this is

An AI task delegation pipeline: Todoist tasks labeled `@delegate` get planned, approved via Telegram, executed by the Claude Agent SDK, and results sent back for review.

## Architecture

```
Todoist (@delegate) → Poller → Planner → [Telegram approval] → Router → Dispatcher (Agent SDK) → Delivery → [Telegram review] → Todoist (complete)
```

At any approval/review step, the user can give feedback which enters **conversing mode** — a direct LLM chat (with web search) via Telegram. Only "go" triggers the agent.

## Key files

| File | Role |
|------|------|
| `src/main.py` | Event loop, Telegram reply routing, state transitions |
| `src/state.py` | Persistent state machine (`state.json`). All status transitions live here. |
| `src/poller.py` | Fetches `@delegate` tasks from Todoist, downloads attachments |
| `src/planner.py` | Generates execution plans via Claude API (not Agent SDK) |
| `src/chatbot.py` | Direct LLM chat with web search for conversing mode |
| `src/router.py` | Classifies task type (research/writing/code/web_form), builds agent prompts |
| `src/dispatcher.py` | Runs Claude Agent SDK with `bypassPermissions`, collects results |
| `src/delivery.py` | Sends results via Telegram, marks tasks complete in Todoist |
| `src/telegram.py` | All Telegram I/O: send plans/results/errors, poll for replies |
| `src/config.py` | Loads `.env`, validates required fields |

## Task state lifecycle

```
new → planning → awaiting_approval → processing → awaiting_review → completed
               ↘ conversing ↗       ↘ conversing ↗  → failed
                                                     → waiting_for_human → processing
```

- `conversing`: user is chatting with LLM (not the agent) to refine requirements. "go" exits this state.
- `waiting_for_human`: agent paused because it needs the user to do something (login, CAPTCHA, etc). "done" resumes.
- Terminal: `completed`, `failed`
- Skipped by poller: all of the above except `processing` and `new`

## State persistence

All state is in `{WORKING_DIR}/state.json`. Conversation history (including raw API content blocks with web search results) is stored in state for `conversing` tasks.

## How Telegram reply routing works (`handle_telegram_reply` in main.py)

Priority order matters — if multiple tasks are in different states, the highest-priority state wins:

1. **conversing** — forward to LLM chatbot, or "go" to dispatch
2. **awaiting_approval** — "go" to process, anything else enters conversing
3. **waiting_for_human** — "done" resumes agent
4. **awaiting_review** — "go"/"done" completes task, anything else enters conversing

## Conventions

- Claude API calls (planner, chatbot) use `httpx` directly against `api.anthropic.com`
- Agent execution uses `claude_agent_sdk.query()`
- The chatbot uses Anthropic's server-side `web_search_20250305` tool
- Approval words: `go, ok, yes, approve, approved, lgtm, looks good, done`
- Per-task working dirs: `{WORKING_DIR}/task-{task_id}/`
- Plans saved to `task-{task_id}/plan.md`

## Common changes

- **Add a new task type**: edit `router.py` — add to the classification prompt and add a system prompt section
- **Change what the chatbot knows**: edit `CHAT_SYSTEM_PROMPT` in `chatbot.py`
- **Change state transitions**: edit `state.py` for the state methods, `main.py` for the routing logic
- **Add new Telegram message types**: edit `telegram.py`
