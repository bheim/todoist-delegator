"""Telegram bot for human communication (plans, human-in-the-loop, results)."""

import asyncio
import time

from telegram import Bot

from .config import Config

TELEGRAM_MAX_LENGTH = 4096


class TelegramBot:
    def __init__(self, config: Config):
        self.bot = Bot(token=config.telegram_bot_token)
        self.chat_id = config.telegram_chat_id
        self._update_offset: int | None = None

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> int:
        """Send a message, splitting if it exceeds Telegram's limit. Returns last message_id."""
        chunks = _split_message(text)
        msg_id = 0
        for chunk in chunks:
            try:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id, text=chunk, parse_mode=parse_mode,
                )
            except Exception:
                # Fallback without parse_mode if formatting fails
                msg = await self.bot.send_message(
                    chat_id=self.chat_id, text=chunk,
                )
            msg_id = msg.message_id
        return msg_id

    async def send_plan(self, task_id: str, task_title: str, plan_text: str) -> int:
        """Send a plan for approval. Returns message_id for reply tracking."""
        text = (
            f"*New task plan*\n"
            f"Task: {_escape_md(task_title)}\n"
            f"ID: `{task_id}`\n\n"
            f"{plan_text}\n\n"
            f'Reply "go" to approve, or send feedback to refine the plan.'
        )
        # Flush pending updates before sending so poll_for_reply only sees new messages
        await self._flush_updates()
        return await self.send_message(text)

    async def send_needs_human(self, task_id: str, task_title: str, message: str) -> int:
        """Send a NEEDS_HUMAN notification. Returns message_id for reply tracking."""
        text = (
            f"*Action needed*\n"
            f"Task: {_escape_md(task_title)}\n"
            f"ID: `{task_id}`\n\n"
            f"{message}\n\n"
            f'Reply "done" when you\'ve completed this action.'
        )
        await self._flush_updates()
        return await self.send_message(text)

    async def send_result(self, task_id: str, task_title: str, success: bool,
                          summary: str, output_files: list[str], cost_usd: float) -> None:
        """Send task results."""
        status = "Completed" if success else "Failed"
        file_list = "\n".join(f"- `{f}`" for f in output_files) if output_files else "(none)"
        text = (
            f"*Result: {status}*\n"
            f"Task: {_escape_md(task_title)}\n"
            f"ID: `{task_id}`\n\n"
            f"*Summary:*\n{summary}\n\n"
            f"*Output files:*\n{file_list}\n\n"
            f"*Cost:* ${cost_usd:.4f}\n\n"
        )
        if success:
            text += 'Please review the work. Reply "done" to mark complete, or send feedback to have the agent try again.'
        await self._flush_updates()
        await self.send_message(text)

    async def send_error(self, task_id: str, task_title: str, error: str) -> None:
        """Send an error notification."""
        text = (
            f"*Error*\n"
            f"Task: {_escape_md(task_title)}\n"
            f"ID: `{task_id}`\n\n"
            f"{error}"
        )
        await self.send_message(text)

    async def _flush_updates(self) -> None:
        """Consume all pending updates so poll_for_reply only sees new messages.

        Only call this right before sending an outbound message that expects a reply.
        """
        updates = await self.bot.get_updates(
            offset=self._update_offset, timeout=0,
        )
        if updates:
            self._update_offset = updates[-1].update_id + 1

    async def poll_for_reply(self, timeout: float = 600.0) -> str | None:
        """Poll for the next message from the user in this chat. Returns text or None on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(1, int(deadline - time.time()))
            poll_timeout = min(10, remaining)
            updates = await self.bot.get_updates(
                offset=self._update_offset, timeout=poll_timeout,
            )
            for update in updates:
                self._update_offset = update.update_id + 1
                if (
                    update.message
                    and str(update.message.chat_id) == str(self.chat_id)
                    and update.message.text
                ):
                    return update.message.text
            if not updates:
                await asyncio.sleep(2)
        return None


def _escape_md(text: str) -> str:
    """Escape special Markdown characters in text."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _split_message(text: str) -> list[str]:
    """Split a message into chunks that fit Telegram's 4096-char limit."""
    if len(text) <= TELEGRAM_MAX_LENGTH:
        return [text]
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MAX_LENGTH:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
        if split_at == -1:
            split_at = TELEGRAM_MAX_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
