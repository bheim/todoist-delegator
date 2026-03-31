"""Direct LLM chat for refining task requirements before dispatching to an agent."""

import httpx

from .config import Config

MODEL_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}

CHAT_SYSTEM_PROMPT = """\
You are a helpful assistant embedded in a task delegation system. The user is \
refining what they want an AI agent to do before it runs.

You have context about the task (title, current plan, and whether results have \
already been produced). Help the user think through what they need — ask \
clarifying questions, suggest improvements to the plan, or discuss the results.

You have access to web search — use it when the user mentions tools, libraries, \
services, or anything you're not sure about. Don't guess when you can look it up.

Keep your responses concise and conversational — this is a Telegram chat. \
When the user is satisfied, remind them to say "go" to launch the agent.

Do NOT execute the task yourself. Your job is only to help the user clarify \
what they want.
"""


class Chatbot:
    def __init__(self, config: Config):
        self.api_key = config.anthropic_api_key
        self.model = MODEL_MAP.get(config.agent_model, config.agent_model)

    async def chat(self, task_content: str, plan: str, conversation_history: list[dict],
                   from_status: str, result_summary: str = "") -> str:
        """Send a message in an ongoing conversation and return the assistant's reply.

        conversation_history should already include the latest user message.
        """
        # Build context about the task
        context_parts = [f"Task: {task_content}"]
        if plan:
            context_parts.append(f"Current plan:\n{plan}")
        if from_status == "awaiting_review":
            context_parts.append("The agent has already run and produced results. The user is reviewing them.")
            if result_summary:
                context_parts.append(f"Agent results:\n{result_summary}")
        else:
            context_parts.append("The agent has not run yet. The user is reviewing the plan.")

        system = CHAT_SYSTEM_PROMPT + "\n\n## Task Context\n" + "\n\n".join(context_parts)

        # Conversation history may contain mixed content blocks from previous
        # tool-use turns. Normalise so each message has the right shape for the API.
        messages = _normalise_history(conversation_history)

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
                    "max_tokens": 2048,
                    "system": system,
                    "messages": messages,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            raw_content = data["content"]

            # Extract text from the response, which may contain interleaved
            # text and web search result blocks.
            text_parts = [
                block["text"]
                for block in raw_content
                if block["type"] == "text" and block.get("text")
            ]
            display_text = "\n".join(text_parts).strip()

            # Return both: display_text for Telegram, raw_content for conversation history.
            # raw_content preserves web_search_tool_result blocks so the API
            # can validate follow-up turns correctly.
            return display_text, raw_content


def _normalise_history(history: list[dict]) -> list[dict]:
    """Ensure conversation history messages are valid for the API.

    When we store assistant turns that used web_search, the content will be a
    list of blocks (text + tool_use + server_tool_use etc). We keep those as-is.
    Plain string messages get passed through unchanged — the API accepts both.
    """
    normalised = []
    for msg in history:
        # Skip empty messages
        content = msg.get("content")
        if not content:
            continue
        normalised.append({"role": msg["role"], "content": content})
    return normalised
