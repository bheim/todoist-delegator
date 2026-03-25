"""Configuration loader for Todoist Delegator."""

import os
import platform
from dataclasses import dataclass, field
from dotenv import load_dotenv


def _default_chrome_profile() -> str:
    system = platform.system()
    if system == "Darwin":
        return "~/Library/Application Support/Google/Chrome/Default"
    elif system == "Windows":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data", "Default")
    else:  # Linux
        return "~/.config/google-chrome/Default"


@dataclass
class Config:
    # Required
    todoist_api_token: str = ""
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Optional with defaults
    chrome_profile_path: str = ""
    delegate_label_name: str = "delegate"
    poll_interval_seconds: int = 30
    agent_model: str = "haiku"
    agent_max_turns: int = 50
    working_dir: str = "./agent-workspace"

    _required_fields: list[str] = field(
        default_factory=lambda: [
            "todoist_api_token",
            "anthropic_api_key",
            "telegram_bot_token",
            "telegram_chat_id",
        ],
        repr=False,
    )

    def validate(self) -> list[str]:
        """Return a list of missing required config values."""
        return [f for f in self._required_fields if not getattr(self, f)]


def load_config() -> Config:
    """Load config from environment variables."""
    load_dotenv()

    return Config(
        todoist_api_token=os.getenv("TODOIST_API_TOKEN", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        chrome_profile_path=os.path.expanduser(
            os.getenv("CHROME_PROFILE_PATH", _default_chrome_profile())
        ),
        delegate_label_name=os.getenv("DELEGATE_LABEL_NAME", "delegate"),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
        agent_model=os.getenv("AGENT_MODEL", "haiku"),
        agent_max_turns=int(os.getenv("AGENT_MAX_TURNS", "50")),
        working_dir=os.path.abspath(
            os.getenv("WORKING_DIR", "./agent-workspace")
        ),
    )
