"""Planner: generates execution plans via Claude API."""

import re

import httpx

from .config import Config
from .poller import DelegatedTask

BROWSER_KEYWORDS = re.compile(
    r"https?://|submit|fill\s*out|website|portal|form|sign\s*up|register|upload\s*to|download\s*from",
    re.IGNORECASE,
)

OUTPUT_DIR_PATTERN = re.compile(
    r"output\s*(?:dir(?:ectory)?|path|folder)?\s*:\s*(.+)",
    re.IGNORECASE,
)

OUTPUT_DIR_NATURAL = re.compile(
    r"(?:save|add|put|write|place|deliver|export)\s+(?:it\s+|this\s+|files?\s+|output\s+|results?\s+)?(?:as\s+\w+\s+)?(?:to|in|on|into)\s+(?:my\s+)?(~/\S+)",
    re.IGNORECASE,
)

MODEL_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}

PLAN_SYSTEM_PROMPT = """\
You are a task planner for an AI agent delegation system. Given a task description, \
create a concise execution plan that another AI agent will follow.

Include:
- Approach: what strategy to use
- Steps: numbered list of concrete actions
- Tools/resources needed (web search, browser automation, file creation, etc.)
- Expected output format and where to save results

If the task involves visiting websites, filling forms, or browser interaction, note that explicitly.
If the task mentions saving files somewhere specific, include that.

Keep the plan brief but actionable — 5-15 lines max. Be specific about what to do, not vague.
"""


class Planner:
    def __init__(self, config: Config):
        self.api_key = config.anthropic_api_key
        self.model = MODEL_MAP.get(config.agent_model, config.agent_model)
        self.working_dir = config.working_dir

    async def generate_plan(self, task: DelegatedTask, feedback: str | None = None) -> str:
        """Generate an execution plan via a single Claude API call."""
        context = f"Task: {task.content}"
        if task.description:
            context += f"\nDescription: {task.description}"
        if task.comments:
            context += f"\nExisting comments: {'; '.join(task.comments)}"

        if feedback:
            context += (
                f"\n\nA previous plan was generated but the user wants changes. "
                f"Here is their feedback:\n{feedback}\n\n"
                f"Please generate a revised plan incorporating this feedback."
            )

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1024,
                    "system": PLAN_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": context}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"].strip()

    def save_plan(self, task_id: str, plan_text: str) -> str:
        """Save the plan to the task working directory. Returns the file path."""
        from pathlib import Path
        task_dir = Path(self.working_dir) / f"task-{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        plan_path = task_dir / "plan.md"
        plan_path.write_text(plan_text)
        return str(plan_path)

    @staticmethod
    def looks_like_browser_task(task: DelegatedTask) -> bool:
        """Check if task content/description suggests browser work."""
        text = f"{task.content} {task.description}"
        return bool(BROWSER_KEYWORDS.search(text))

    @staticmethod
    def extract_output_dir(task: DelegatedTask) -> str | None:
        """Extract an output directory from task content/description if specified."""
        for text in [task.content, task.description] + task.comments:
            match = OUTPUT_DIR_PATTERN.search(text)
            if match:
                return match.group(1).strip()
            match = OUTPUT_DIR_NATURAL.search(text)
            if match:
                return match.group(1).strip()
        return None
