# Todoist Delegator

Watches for Todoist tasks with a `@delegate` label, clarifies the task via Todoist comments, runs it through the Claude Agent SDK, and delivers results back as comments.

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
  → Clarifier (asks 2-3 questions via Todoist comments)
  → Router (classifies: research | writing | code | web_form)
  → Dispatcher (runs Claude Agent SDK)
  → Delivery (Todoist comment + mark complete)
```

1. **Create a task** in Todoist with the label `delegate`
2. The service posts clarifying questions as comments — reply to each one
3. It classifies your task (research, writing, code, or web_form)
4. An AI agent executes the task autonomously
5. If it needs your help (e.g., login), it pauses and asks via a comment — reply `done` when ready
6. Results are posted as a comment and the task is marked complete

## Task Types

| Type | What it does | Tools |
|------|-------------|-------|
| `research` | Finds info, compares options | Bash, WebSearch, Read, Write |
| `writing` | Drafts emails, docs, reports | Read, Write, Edit, WebSearch |
| `code` | Writes scripts, builds features | Bash, Read, Write, Edit, Glob, Grep |
| `web_form` | Browser automation, form filling | Bash, Read, Write + agent-browser |

## Browser Tasks

For `web_form` tasks, the service opens a **visible browser window** so you can watch the agent work. If it hits a login page or needs your input, it pauses and asks you via a Todoist comment.

You can also use your logged-in Chrome session by replying `my browser` when asked during clarification. This requires Chrome to be closed so `agent-browser` can access the profile.

## Configuration

All config is in `.env` (see `.env.example`):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TODOIST_API_TOKEN` | Yes | | Your Todoist API token |
| `ANTHROPIC_API_KEY` | Yes | | Your Anthropic API key |
| `DELEGATE_LABEL_NAME` | No | `delegate` | Todoist label to watch |
| `POLL_INTERVAL_SECONDS` | No | `30` | How often to check for new tasks |
| `AGENT_MODEL` | No | `sonnet` | Claude model (sonnet/opus/haiku) |
| `AGENT_MAX_TURNS` | No | `50` | Max agent turns per task |
| `WORKING_DIR` | No | `./agent-workspace` | Where task files are stored |
| `CHROME_PROFILE_PATH` | No | Auto-detected | Path to Chrome profile for user browser mode |

## State Tracking

Task state is persisted to `{WORKING_DIR}/state.json`. If the service restarts:
- **Completed/failed** tasks are skipped
- **In-progress** tasks resume where they left off (no duplicate questions)
- **Waiting for human** tasks check for your `done` reply each poll cycle
