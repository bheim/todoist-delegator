"""Router: classifies task type and builds agent prompts."""

import json
from dataclasses import dataclass

import httpx

from .config import Config

TASK_TYPES = {
    "research": {
        "tools": ["Bash", "WebSearch", "Read", "Write"],
        "system_prompt": (
            "You are a research assistant. Find information, compare options, "
            "and produce a clear, well-organized summary. Save your findings "
            "to a file in the working directory."
        ),
    },
    "writing": {
        "tools": ["Read", "Write", "Edit", "WebSearch"],
        "system_prompt": (
            "You are a writing assistant. Draft polished, professional content. "
            "Save your output to a file in the working directory."
        ),
    },
    "code": {
        "tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        "system_prompt": (
            "You are a coding assistant. Write clean, well-structured code. "
            "Test your work when possible. Save all code to files in the working directory."
        ),
    },
}

WEB_FORM_TOOLS = ["Bash", "Read", "Write"]

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
You are a task classifier. Given a task description and clarification context, \
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

    async def classify(self, clarification: dict) -> str:
        """Classify the task into a type using Claude API."""
        task = clarification["task"]
        context = f"Task: {task.content}"
        if task.description:
            context += f"\nDescription: {task.description}"
        if clarification["questions"] and clarification["answers"]:
            qa_pairs = zip(clarification["questions"], clarification["answers"])
            context += "\n\nClarification Q&A:"
            for q, a in qa_pairs:
                context += f"\nQ: {q}\nA: {a}"

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
            text = data["content"][0]["text"]
            result = json.loads(text)
            task_type = result["type"]

        valid_types = set(TASK_TYPES) | {"web_form"}
        if task_type not in valid_types:
            task_type = "research"
        return task_type

    def build_prompt(self, clarification: dict) -> str:
        """Assemble the full agent prompt from task context and Q&A."""
        task = clarification["task"]
        parts = [f"# Task\n{task.content}"]

        if task.description:
            parts.append(f"## Description\n{task.description}")

        if task.comments:
            parts.append("## Existing Comments\n" + "\n".join(f"- {c}" for c in task.comments))

        if clarification["questions"] and clarification["answers"]:
            qa_section = "## Clarification Q&A"
            for q, a in zip(clarification["questions"], clarification["answers"]):
                qa_section += f"\n**Q:** {q}\n**A:** {a}"
            parts.append(qa_section)

        if task.attachments:
            att_section = "## Attached Files\nThe following files have been downloaded to your working directory:"
            for path in task.attachments:
                att_section += f"\n- `{path}`"
            parts.append(att_section)

        parts.append(
            "\n## Instructions\n"
            "Complete this task thoroughly. Save all output files to the current working directory. "
            "When finished, provide a clear summary of what you did and any output files created."
        )

        return "\n\n".join(parts)

    async def route(self, clarification: dict) -> RoutedTask:
        """Classify the task and build the routed task with tools and prompts."""
        task_type = await self.classify(clarification)
        use_user_browser = clarification.get("use_user_browser", False)

        if task_type == "web_form":
            tools = WEB_FORM_TOOLS
            if use_user_browser:
                system_prompt = WEB_FORM_USER_BROWSER_PROMPT.format(
                    chrome_profile_path=self.chrome_profile_path
                )
            else:
                system_prompt = WEB_FORM_HEADLESS_PROMPT
        else:
            type_config = TASK_TYPES[task_type]
            tools = type_config["tools"]
            system_prompt = type_config["system_prompt"]

        agent_prompt = self.build_prompt(clarification)

        return RoutedTask(
            task_type=task_type,
            tools=tools,
            system_prompt=system_prompt,
            agent_prompt=agent_prompt,
            use_user_browser=use_user_browser,
        )
