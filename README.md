# Todoist Delegator

Watches for Todoist tasks with a `@delegate` label that are **due today or overdue**, generates an execution plan, gets your approval via Telegram, runs the task through the Claude Agent SDK, and sends results back for your review — all through Telegram.

## Quick Start

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd todoist-delegator

# 2. Run setup (creates venv, installs deps, walks you through config)
python3 setup.py

# 3. Verify everything is working
python3 verify_setup.py

# 4. Run
source .venv/bin/activate
python -m src.main
```

Setup will open the right pages in your browser for each API token and auto-detect your Telegram chat ID — just follow the prompts.

## Requirements

- Python 3.10+
- Node.js (for `agent-browser` — installed automatically by setup)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## How It Works

```
Todoist (poll for @delegate label, due today or overdue)
  → Planner (generates execution plan via Claude API)
  → Telegram (sends plan for your approval)
  → Router (classifies: research | writing | code | web_form)
  → Dispatcher (runs Claude Agent SDK)
  → Telegram (sends results for your review)
  → Todoist (mark complete)
```

1. **Create a task** in Todoist with the label `delegate` and a **due date**
2. When the task is due (today or overdue), the delegator picks it up and sends you a plan on Telegram — reply **"go"** to approve, or send feedback to start a conversation
3. If you give feedback, you enter a **chat mode** where you talk directly with an LLM (with web search) to refine what you want. Say **"go"** when you're ready to launch the agent.
4. The agent executes the task autonomously
5. If the agent needs your help (e.g., login, CAPTCHA), it pauses and asks via Telegram — reply **"done"** when ready
6. Results are sent to Telegram — reply **"done"** to mark complete, or send feedback to chat and refine before re-running

> **Note:** Tasks without a due date or with a future due date are ignored until they become due. This makes the delegator ideal for recurring tasks — set up a recurring Todoist task with the `delegate` label and it will be picked up each time it comes due.

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

## Running as a Background Service (macOS)

To keep the delegator running at all times — surviving reboots and auto-restarting on crashes — use macOS `launchd`.

### Install and start

```bash
# Generate and install the launchd plist
python3 install_service.py

# Start the service
launchctl load ~/Library/LaunchAgents/com.todoist-delegator.plist
```

### Manage the service

```bash
# Check status (healthy = has a PID and exit status 0)
launchctl list | grep todoist

# View logs
tail -f agent-workspace/delegator.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.todoist-delegator.plist

# Restart after config changes
launchctl unload ~/Library/LaunchAgents/com.todoist-delegator.plist
launchctl load ~/Library/LaunchAgents/com.todoist-delegator.plist
```

## Verifying Your Setup

Run the verification script at any time to check that everything is configured correctly:

```bash
python3 verify_setup.py
```

This checks:
- Prerequisites (Python, Node.js, Claude Code CLI, agent-browser)
- All required environment variables are set
- Todoist API token is valid and the `delegate` label exists
- Anthropic API key is valid
- Telegram bot token works and can send messages to your chat ID
- Background service is installed and running (macOS)

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
| `AGENT_MODEL` | No | `haiku` | Claude model (`haiku`/`sonnet`/`opus`) |
| `AGENT_MAX_TURNS` | No | `50` | Max agent turns per task |
| `WORKING_DIR` | No | `./agent-workspace` | Where task files are stored |
| `CHROME_PROFILE_PATH` | No | Auto-detected | Path to Chrome profile for user browser mode |

## State Tracking

Task state is persisted to `{WORKING_DIR}/state.json`. If the service restarts:
- **Completed/failed** tasks are skipped
- **In-progress** tasks resume where they left off
- **Conversing** tasks preserve your chat history — pick up where you left off
- **Waiting for human** tasks check for your "done" reply each poll cycle
