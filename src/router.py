"""Router: classifies task type and builds agent prompts."""

import json
import re
from dataclasses import dataclass

import httpx

from .config import Config

ALL_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch"]

TASK_TYPES = {
    "research": {
        "system_prompt": (
            "You are a capable assistant. Your primary goal is to find information, "
            "compare options, and produce a clear summary. Use all tools at your disposal — "
            "search the web, run shell commands, read and write files. "
            "Actually do the work; do not just describe steps for the user to follow."
        ),
    },
    "writing": {
        "system_prompt": (
            "You are a capable assistant. Your primary goal is to draft polished, "
            "professional content. Use all tools at your disposal. "
            "Save your output to a file in the working directory."
        ),
    },
    "code": {
        "system_prompt": (
            "You are a capable assistant. Your primary goal is to write code, set up projects, "
            "configure infrastructure, or solve technical problems. Use all tools at your disposal — "
            "run shell commands, install packages, clone repos, SSH into servers, etc. "
            "Actually do the work; do not just write instructions for the user to follow."
        ),
    },
}

WEB_FORM_USER_BROWSER_PROMPT = """\
You are a web automation specialist using the user's authenticated Chrome session.
Use `agent-browser` with `--profile {chrome_profile_path}` on all `open` commands.
You are already logged in — do NOT attempt to log in or enter credentials.
If you hit a login wall unexpectedly, stop and report it.

Workflow:
1. agent-browser open <url> --headed --profile {chrome_profile_path}
2. agent-browser snapshot -i
3. agent-browser fill @ref "value"
4. agent-browser click @ref
5. agent-browser screenshot (always verify after important actions)
"""

WEB_FORM_HEADLESS_PROMPT = """\
You are a web automation specialist running in a visible browser window.
The browser is headed — the user can see what you're doing.

If you need the user to do something (login, 2FA, CAPTCHA, make a choice, etc.):
1. STOP immediately
2. In your final response, start with "NEEDS_HUMAN: " followed by a clear description of what the user needs to do. Example: "NEEDS_HUMAN: Please log in to UChicago Okta in the browser window."
3. Do NOT continue or try to work around it. The system will notify the user and resume you after they're done.

Do NOT mark the task as complete until the actual form is filled out and submitted.

Workflow:
1. agent-browser open <url> --headed
2. agent-browser snapshot -i
3. agent-browser fill @ref "value"
4. agent-browser click @ref
5. agent-browser screenshot (always verify after important actions)

IMPORTANT: Always pass --headed to `agent-browser open` so the browser window is visible to the user.
"""

CLASSIFIER_SYSTEM_PROMPT = """\
You are a task classifier. Given a task description and its execution plan, \
classify it into exactly one of these types: research, writing, code, web_form.

- research: finding information, comparing options, market analysis
- writing: drafting emails, documents, reports, blog posts
- code: writing scripts, building features, fixing bugs
- web_form: filling out forms, browser automation, web interactions

Respond with ONLY a JSON object: {"type": "<type>"}
"""


@dataclass
class RoutedTask:
    task_type: str
    tools: list[str]
    system_prompt: str
    agent_prompt: str
    use_user_browser: bool
    output_dir: str | None = None


MODEL_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}


class Router:
    def __init__(self, config: Config):
        self.api_key = config.anthropic_api_key
        self.model = MODEL_MAP.get(config.agent_model, config.agent_model)
        self.chrome_profile_path = config.chrome_profile_path

    async def classify(self, plan_context: dict) -> str:
        """Classify the task into a type using Claude API."""
        task = plan_context["task"]
        context = f"Task: {task.content}"
        if task.description:
            context += f"\nDescription: {task.description}"
        if plan_context.get("plan"):
            context += f"\n\nExecution plan:\n{plan_context['plan']}"

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
                    "max_tokens": 100,
                    "system": CLASSIFIER_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": context}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["content"][0]["text"].strip()

            # Strip markdown code fences if present
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*\n?", "", text)
                text = re.sub(r"\n?```\s*$", "", text)

            # Extract JSON object if surrounded by other text
            brace_start = text.find("{")
            brace_end = text.rfind("}")
            if brace_start != -1 and brace_end != -1:
                text = text[brace_start : brace_end + 1]

            try:
                result = json.loads(text)
                task_type = result["type"]
            except (json.JSONDecodeError, KeyError):
                print(f"    [warn] Could not parse task type from API, raw: {text[:200]}")
                task_type = "research"

        valid_types = set(TASK_TYPES) | {"web_form"}
        if task_type not in valid_types:
            task_type = "research"
        return task_type

    def build_prompt(self, plan_context: dict) -> str:
        """Assemble the full agent prompt from task context and plan."""
        task = plan_context["task"]
        parts = [f"# Task\n{task.content}"]

        if task.description:
            parts.append(f"## Description\n{task.description}")

        if task.comments:
            parts.append("## Existing Comments\n" + "\n".join(f"- {c}" for c in task.comments))

        if plan_context.get("plan"):
            parts.append(f"## Execution Plan\n{plan_context['plan']}")

        if task.attachments:
            att_section = "## Attached Files\nThe following files have been downloaded to your working directory:"
            for path in task.attachments:
                att_section += f"\n- `{path}`"
            parts.append(att_section)

        parts.append(
            "\n## Instructions\n"
            "Follow the execution plan above. Actually do the work — run commands, install software, "
            "download files, configure systems, etc. Do NOT just write markdown instructions or "
            "step-by-step guides for the user to follow. Save all output files to the current "
            "working directory. When finished, provide a clear summary of what you did."
        )

        return "\n\n".join(parts)

    async def route(self, plan_context: dict) -> RoutedTask:
        """Classify the task and build the routed task with tools and prompts."""
        task_type = await self.classify(plan_context)
        use_user_browser = plan_context.get("use_user_browser", False)

        if task_type == "web_form":
            tools = ALL_TOOLS
            if use_user_browser:
                system_prompt = WEB_FORM_USER_BROWSER_PROMPT.format(
                    chrome_profile_path=self.chrome_profile_path
                )
            else:
                system_prompt = WEB_FORM_HEADLESS_PROMPT
        else:
            type_config = TASK_TYPES[task_type]
            tools = ALL_TOOLS
            system_prompt = type_config["system_prompt"]

        agent_prompt = self.build_prompt(plan_context)

        return RoutedTask(
            task_type=task_type,
            tools=tools,
            system_prompt=system_prompt,
            agent_prompt=agent_prompt,
            use_user_browser=use_user_browser,
            output_dir=plan_context.get("output_dir"),
        )
