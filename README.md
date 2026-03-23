# Todoist Delegator

Watches for Todoist tasks with a `@delegate` label, generates an execution plan, gets your approval via Telegram, runs the task through the Claude Agent SDK, and sends results back for your review — all through Telegram.

## Quick Start

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd todoist-delegator

# 2. Run setup (creates venv, installs deps, generates .env)
python3 setup.py

# 3. Fill in your API tokens in .env
#    - TODOIST_API_TOKEN: https://app.todoist.com/app/settings/integrations/developer
#    - ANTHROPIC_API_KEY: https://console.anthropic.com/
#    - TELEGRAM_BOT_TOKEN: create a bot via @BotFather on Telegram
#    - TELEGRAM_CHAT_ID: your chat ID (message @userinfobot to find it)

# 4. Run
source .venv/bin/activate
python -m src.main
```

## Requirements

- Python 3.10+
- Node.js (for `agent-browser` — installed automatically by setup)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## How It Works

```
Todoist (poll for @delegate label)
  → Planner (generates execution plan via Claude API)
  → Telegram (sends plan for your approval)
  → Router (classifies: research | writing | code | web_form)
  → Dispatcher (runs Claude Agent SDK)
  → Telegram (sends results for your review)
  → Todoist (mark complete)
```

1. **Create a task** in Todoist with the label `delegate`
2. You get a plan on Telegram — reply **"go"** to approve, or send feedback to start a conversation
3. If you give feedback, you enter a **chat mode** where you talk directly with an LLM (with web search) to refine what you want. Say **"go"** when you're ready to launch the agent.
4. The agent executes the task autonomously
5. If the agent needs your help (e.g., login, CAPTCHA), it pauses and asks via Telegram — reply **"done"** when ready
6. Results are sent to Telegram — reply **"done"** to mark complete, or send feedback to chat and refine before re-running

## Chat Mode

When you reply with feedback (anything other than "go"/"done"/etc.) at the plan or review stage, you enter a conversational mode. This lets you:

- **Talk through what you need** with an LLM before spending agent compute
- **Ask questions** — the LLM has web search, so it can look up tools, libraries, or services you mention
- **Iterate on requirements** across multiple messages

The conversation history is passed to the agent when you finally say "go", so nothing is lost.

## Task Types

| Type | What it does | Tools |
|------|-------------|-------|
| `research` | Finds info, compares options | Bash, WebSearch, Read, Write |
| `writing` | Drafts emails, docs, reports | Read, Write, Edit, WebSearch |
| `code` | Writes scripts, builds features | Bash, Read, Write, Edit, Glob, Grep |
| `web_form` | Browser automation, form filling | Bash, Read, Write + agent-browser |

## Browser Tasks

For `web_form` tasks, the agent can run a headless browser or use your logged-in Chrome session. If it hits a login page or needs your input, it pauses and asks via Telegram.

Using your Chrome session requires Chrome to be closed so `agent-browser` can access the profile.

## Configuration

All config is in `.env` (see `.env.example`):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TODOIST_API_TOKEN` | Yes | | Your Todoist API token |
| `ANTHROPIC_API_KEY` | Yes | | Your Anthropic API key |
| `TELEGRAM_BOT_TOKEN` | Yes | | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | | Your Telegram chat ID |
| `DELEGATE_LABEL_NAME` | No | `delegate` | Todoist label to watch |
| `POLL_INTERVAL_SECONDS` | No | `30` | How often to check for new tasks |
| `AGENT_MODEL` | No | `sonnet` | Claude model (sonnet/opus/haiku) |
| `AGENT_MAX_TURNS` | No | `50` | Max agent turns per task |
| `WORKING_DIR` | No | `./agent-workspace` | Where task files are stored |
| `CHROME_PROFILE_PATH` | No | Auto-detected | Path to Chrome profile for user browser mode |

## State Tracking

Task state is persisted to `{WORKING_DIR}/state.json`. If the service restarts:
- **Completed/failed** tasks are skipped
- **In-progress** tasks resume where they left off
- **Conversing** tasks preserve your chat history — pick up where you left off
- **Waiting for human** tasks check for your "done" reply each poll cycle
