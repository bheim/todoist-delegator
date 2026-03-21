"""Clarifier: generates questions via Claude API, asks them via Todoist comments."""

import asyncio
import json
import re
import time

import httpx
from todoist_api_python.api import TodoistAPI

from .config import Config
from .poller import DelegatedTask

BROWSER_KEYWORDS = re.compile(
    r"https?://|submit|fill\s*out|website|portal|form|sign\s*up|register|upload\s*to|download\s*from",
    re.IGNORECASE,
)

BROWSER_QUESTION = (
    "This looks like it needs a browser. Should I use your logged-in Chrome session, "
    "or go headless? (reply `my browser` or `headless`)"
)

DELEGATOR_HEADER = "🤖 **Delegator**"

QUESTION_SYSTEM_PROMPT = """\
You are a task clarifier. Given a task description, generate 2-3 short, \
specific clarifying questions that will help an AI agent execute this task \
successfully. Focus on ambiguities, missing details, and scope.

Respond with ONLY a JSON array of question strings. Example:
["What format should the output be in?", "Should I include pricing comparisons?"]
"""


# Map config model names to API model IDs
MODEL_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}


class Clarifier:
    def __init__(self, config: Config):
        self.api_key = config.anthropic_api_key
        self.model = MODEL_MAP.get(config.agent_model, config.agent_model)
        self.todoist = TodoistAPI(config.todoist_api_token)

    async def _generate_questions(self, task: DelegatedTask) -> list[str]:
        """Call Claude API to generate clarifying questions."""
        task_context = f"Task: {task.content}"
        if task.description:
            task_context += f"\nDescription: {task.description}"
        if task.comments:
            task_context += f"\nExisting comments: {'; '.join(task.comments)}"

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
                    "max_tokens": 300,
                    "system": QUESTION_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": task_context}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["content"][0]["text"]
            return json.loads(text)

    def _flatten_comments(self, task_id: str) -> list:
        """Get all comments for a task, flattening paginated results."""
        comments = []
        for page in self.todoist.get_comments(task_id=task_id):
            comments.extend(page)
        return comments

    def _get_comment_ids(self, task_id: str) -> set[str]:
        """Get current comment IDs for a task."""
        return {c.id for c in self._flatten_comments(task_id)}

    def _wait_for_new_comment(self, task_id: str, known_ids: set[str], timeout: float = 300.0) -> str | None:
        """Poll for a new comment on the task. Returns the comment text or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            for c in self._flatten_comments(task_id):
                if c.id not in known_ids:
                    return c.content
        return None

    @staticmethod
    def _looks_like_browser_task(task: DelegatedTask) -> bool:
        """Check if task content/description suggests browser work."""
        text = f"{task.content} {task.description}"
        return bool(BROWSER_KEYWORDS.search(text))

    def has_existing_clarification(self, task: DelegatedTask) -> bool:
        """Check if this task already has delegator comments from a previous run."""
        return any(c.startswith(DELEGATOR_HEADER) for c in task.comments)

    def parse_existing_clarification(self, task: DelegatedTask) -> dict:
        """Reconstruct a clarification dict from existing Todoist comments.

        Parses the comment thread to extract Q&A pairs that were already posted.
        Comments with '**Q:**' prefix are questions from the bot.
        Comments between questions (that aren't bot messages) are user answers.
        """
        comments = task.comments
        questions = []
        answers = []
        use_user_browser = False

        # Walk through comments, pairing questions with the next non-bot answer
        i = 0
        while i < len(comments):
            c = comments[i]
            if c.startswith("**Q:**"):
                question_text = c[len("**Q:** "):]
                questions.append(question_text)
                # Look for the next comment that isn't a bot comment
                i += 1
                found_answer = False
                while i < len(comments):
                    next_c = comments[i]
                    # Skip bot comments
                    if (
                        next_c.startswith(DELEGATOR_HEADER)
                        or next_c.startswith("**Q:**")
                        or next_c.startswith("_(")
                        or next_c.startswith("⚠️")
                        or next_c.startswith("⏰")
                        or next_c.startswith("🤖")
                    ):
                        break
                    # This is a user answer
                    answers.append(next_c)
                    found_answer = True
                    # Check browser choice
                    if question_text.startswith("This looks like it needs a browser"):
                        if "my browser" in next_c.lower():
                            use_user_browser = True
                    i += 1
                    break
                if not found_answer:
                    answers.append("[no answer]")
            else:
                i += 1

        skipped = any("skip" in c.lower() for c in comments if not c.startswith("**Q:**") and not c.startswith("🤖"))

        return {
            "task": task,
            "questions": questions,
            "answers": answers,
            "skipped": skipped,
            "use_user_browser": use_user_browser,
        }

    async def clarify(self, task: DelegatedTask) -> dict:
        """Run the full clarification flow: generate questions, post as comments, wait for replies."""
        # Post a header comment
        self.todoist.add_comment(
            task_id=task.task_id,
            content=f"{DELEGATOR_HEADER}: I have a few clarifying questions before I start. "
            "Reply to each with a comment. Reply \"skip\" to skip remaining questions.",
        )

        # Generate questions
        questions = await self._generate_questions(task)

        # Append browser session question if task looks like browser work
        looks_like_browser = self._looks_like_browser_task(task)
        if looks_like_browser:
            questions.append(BROWSER_QUESTION)

        answers = []
        skipped = False
        use_user_browser = False

        for q in questions:
            # Post the question
            self.todoist.add_comment(task_id=task.task_id, content=f"**Q:** {q}")

            # Snapshot current comment IDs, then wait for a new one
            known_ids = self._get_comment_ids(task.task_id)
            reply = await asyncio.get_event_loop().run_in_executor(
                None, self._wait_for_new_comment, task.task_id, known_ids
            )

            if reply is None:
                answers.append("[timed out]")
                self.todoist.add_comment(
                    task_id=task.task_id, content="_(timed out, moving on)_"
                )
            elif reply.strip().lower() == "skip":
                skipped = True
                self.todoist.add_comment(
                    task_id=task.task_id, content="_(skipping remaining questions)_"
                )
                break
            else:
                answers.append(reply)
                # Parse browser session choice
                if q == BROWSER_QUESTION and "my browser" in reply.lower():
                    use_user_browser = True

        return {
            "task": task,
            "questions": questions,
            "answers": answers,
            "skipped": skipped,
            "use_user_browser": use_user_browser,
        }
