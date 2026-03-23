#!/usr/bin/env python3
"""Setup script for Todoist Delegator."""

import os
import shutil
import subprocess
import sys
import venv


def run(cmd, **kwargs):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, **kwargs)
    if result.returncode != 0:
        print(f"  FAILED (exit code {result.returncode})")
        return False
    return True


# Each entry: (ENV_VAR_NAME, prompt text, help text shown before prompting)
REQUIRED_CONFIG = [
    (
        "TODOIST_API_TOKEN",
        "Todoist API token",
        "  Get yours at: https://app.todoist.com/app/settings/integrations/developer",
    ),
    (
        "ANTHROPIC_API_KEY",
        "Anthropic API key",
        "  Get yours at: https://console.anthropic.com/",
    ),
    (
        "TELEGRAM_BOT_TOKEN",
        "Telegram bot token",
        "  To create a bot:\n"
        "    1. Open Telegram and message @BotFather\n"
        "    2. Send /newbot and follow the prompts\n"
        "    3. Copy the token it gives you",
    ),
    (
        "TELEGRAM_CHAT_ID",
        "Telegram chat ID",
        "  To get your chat ID:\n"
        "    1. Message @userinfobot on Telegram\n"
        "    2. It replies with your numeric ID\n"
        "  (Also make sure you've sent a message to your new bot so it can reply to you)",
    ),
]


def load_env(env_file):
    """Load existing .env values into a dict."""
    values = {}
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    values[key.strip()] = val.strip()
    return values


def save_env(env_file, values):
    """Write values to .env, preserving comments and optional settings from the template."""
    template = os.path.join(os.path.dirname(env_file), ".env.example")
    lines = []

    if os.path.exists(template):
        with open(template) as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.partition("=")[0].strip()
                    if key in values:
                        lines.append(f"{key}={values[key]}\n")
                        continue
                lines.append(line)
    else:
        for key, val in values.items():
            lines.append(f"{key}={val}\n")

    with open(env_file, "w") as f:
        f.writelines(lines)


def configure_env(env_file):
    """Interactively prompt for missing required config values."""
    values = load_env(env_file)
    missing = [(key, prompt, help_text) for key, prompt, help_text in REQUIRED_CONFIG if not values.get(key)]

    if not missing:
        print("[OK] All required config values are set")
        return

    print(f"\n--- Configuration ({len(missing)} value(s) needed) ---\n")
    print("Press Enter to skip any value (you can set it later in .env).\n")

    for key, prompt, help_text in missing:
        print(help_text)
        val = input(f"  {prompt}: ").strip()
        if val:
            values[key] = val
            print(f"  [OK] {key} set\n")
        else:
            print(f"  [skipped] You'll need to set {key} in .env before running.\n")

    save_env(env_file, values)


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    print("=== Todoist Delegator Setup ===\n")

    # 1. Check Python version
    if sys.version_info < (3, 10):
        print(f"ERROR: Python 3.10+ required, you have {sys.version}")
        sys.exit(1)
    print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}")

    # 2. Check Node.js
    if shutil.which("node"):
        print("[OK] Node.js found")
    else:
        print("[!!] Node.js not found — needed for agent-browser")
        print("     Install from https://nodejs.org/")

    # 3. Check Claude Code CLI
    if shutil.which("claude"):
        print("[OK] Claude Code CLI found")
    else:
        print("[!!] Claude Code CLI not found — needed for agent dispatch")
        print("     Install from https://docs.anthropic.com/en/docs/claude-code")

    # 4. Create virtual environment
    venv_dir = os.path.join(root, ".venv")
    if not os.path.exists(venv_dir):
        print("\nCreating virtual environment...")
        venv.create(venv_dir, with_pip=True)
    print(f"[OK] Virtual environment at .venv/")

    # 5. Install Python dependencies
    pip = os.path.join(venv_dir, "bin", "pip") if os.name != "nt" else os.path.join(venv_dir, "Scripts", "pip.exe")
    print("\nInstalling Python dependencies...")
    run(f"{pip} install -r requirements.txt")

    # 6. Install agent-browser
    if not shutil.which("agent-browser"):
        print("\nInstalling agent-browser...")
        run("npm install -g agent-browser")
    else:
        print("[OK] agent-browser already installed")

    # 7. Create .env from template if missing, then prompt for values
    env_file = os.path.join(root, ".env")
    if not os.path.exists(env_file):
        print("\nCreating .env from template...")
        shutil.copy(os.path.join(root, ".env.example"), env_file)

    configure_env(env_file)

    # 8. Create workspace directory
    os.makedirs(os.path.join(root, "agent-workspace"), exist_ok=True)

    print("\n=== Setup Complete ===\n")

    # Check if any required values are still missing
    values = load_env(env_file)
    still_missing = [key for key, _, _ in REQUIRED_CONFIG if not values.get(key)]
    if still_missing:
        print("Before running, set these in .env:")
        for key in still_missing:
            print(f"  - {key}")
        print()

    print("To run:")
    if os.name == "nt":
        print("  .venv\\Scripts\\activate")
    else:
        print("  source .venv/bin/activate")
    print("  python -m src.main")
    print()


if __name__ == "__main__":
    main()
