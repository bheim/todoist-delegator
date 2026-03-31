"""Telegram bot for human communication (plans, human-in-the-loop, results).

Uses Forum Topics (threads) when chat_id points to a forum-enabled group.
Each task gets its own topic. Falls back to flat chat if topics aren't available.
"""

import asyncio
import time
from pathlib import Path

from telegram import Bot
from telegram.error import BadRequest

from .config import Config

TELEGRAM_MAX_LENGTH = 4096


class TelegramBot:
    def __init__(self, config: Config):
        self.bot = Bot(token=config.telegram_bot_token)
        self.chat_id = config.telegram_chat_id
        self._update_offset: int | None = None
        self._forum_mode: bool | None = None  # auto-detected on first use

    async def _is_forum(self) -> bool:
        """Check if the chat is a forum (has topics enabled)."""
        if self._forum_mode is None:
            try:
                chat = await self.bot.get_chat(self.chat_id)
                self._forum_mode = getattr(chat, "is_forum", False) or False
            except Exception:
                self._forum_mode = False
        return self._forum_mode

    async def create_topic(self, name: str) -> int | None:
        """Create a forum topic and return its thread_id. Returns None if not a forum."""
        if not await self._is_forum():
            return None
        try:
            topic = await self.bot.create_forum_topic(
                chat_id=self.chat_id,
                name=name[:128],  # Telegram limit
            )
            return topic.message_thread_id
        except Exception as e:
            print(f"[warn] Failed to create topic '{name}': {e}")
            return None

    async def delete_topic(self, thread_id: int) -> None:
        """Delete a forum topic."""
        if not thread_id or not await self._is_forum():
            return
        try:
            await self.bot.delete_forum_topic(
                chat_id=self.chat_id,
                message_thread_id=thread_id,
            )
        except Exception as e:
            print(f"[warn] Failed to delete topic {thread_id}: {e}")

    async def send_message(self, text: str, parse_mode: str = "Markdown",
                           thread_id: int | None = None) -> int:
        """Send a message, splitting if it exceeds Telegram's limit. Returns last message_id."""
        chunks = _split_message(text)
        msg_id = 0
        kwargs = {}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        for chunk in chunks:
            try:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id, text=chunk, parse_mode=parse_mode,
                    **kwargs,
                )
            except Exception:
                # Fallback without parse_mode if formatting fails
                msg = await self.bot.send_message(
                    chat_id=self.chat_id, text=chunk,
                    **kwargs,
                )
            msg_id = msg.message_id
        return msg_id

    async def send_file(self, file_path: str, caption: str = "",
                        thread_id: int | None = None) -> None:
        """Send a file via Telegram (up to 50MB)."""
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return
        kwargs = {}
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        with open(path, "rb") as f:
            await self.bot.send_document(
                chat_id=self.chat_id,
                document=f,
                caption=caption[:1024] if caption else path.name,
                **kwargs,
            )

    async def send_plan(self, task_id: str, task_title: str, plan_text: str,
                        nickname: str = "", thread_id: int | None = None) -> int:
        """Send a plan for approval. Returns message_id for reply tracking."""
        text = (
            f"*New task plan*\n"
            f"Task: {_escape_md(task_title)}\n\n"
            f"{plan_text}\n\n"
            f'Reply "go" to approve, or send feedback to refine.'
        )
        return await self.send_message(text, thread_id=thread_id)

    async def send_needs_human(self, task_id: str, task_title: str, message: str,
                              nickname: str = "", thread_id: int | None = None) -> int:
        """Send a NEEDS_HUMAN notification. Returns message_id for reply tracking."""
        text = (
            f"*Action needed*\n"
            f"Task: {_escape_md(task_title)}\n\n"
            f"{message}\n\n"
            f'Reply "done" when you\'ve completed this action.'
        )
        return await self.send_message(text, thread_id=thread_id)

    async def send_result(self, task_id: str, task_title: str, success: bool,
                          summary: str, output_files: list[str], cost_usd: float,
                          nickname: str = "", thread_id: int | None = None) -> None:
        """Send task results."""
        status = "Completed" if success else "Failed"
        file_list = "\n".join(f"- `{f}`" for f in output_files) if output_files else "(none)"
        text = (
            f"*Result: {status}*\n"
            f"Task: {_escape_md(task_title)}\n\n"
            f"*Summary:*\n{summary}\n\n"
            f"*Output files:*\n{file_list}\n\n"
            f"*Cost:* ${cost_usd:.4f}\n\n"
        )
        if success:
            text += 'Reply "done" to complete, or send feedback to refine.'
        await self.send_message(text, thread_id=thread_id)

    async def send_error(self, task_id: str, task_title: str, error: str,
                         nickname: str = "", thread_id: int | None = None) -> None:
        """Send an error notification with retry option."""
        text = (
            f"*Error*\n"
            f"Task: {_escape_md(task_title)}\n\n"
            f"{error}\n\n"
            f'Reply "retry" to try again.'
        )
        await self.send_message(text, thread_id=thread_id)

    async def poll_for_reply(self, timeout: float = 600.0) -> tuple[str, int | None] | None:
        """Poll for the next message from the user in this chat.

        Returns (text, thread_id) or None on timeout.
        thread_id is the forum topic ID if the message was in a topic, else None.
        """
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
                    thread_id = update.message.message_thread_id
                    return update.message.text, thread_id
            if not updates:
                await asyncio.sleep(2)
        return None

    async def _flush_updates(self) -> None:
        """Consume all pending updates so poll_for_reply only sees new messages."""
        updates = await self.bot.get_updates(
            offset=self._update_offset, timeout=0,
        )
        if updates:
            self._update_offset = updates[-1].update_id + 1


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
