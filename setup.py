#!/usr/bin/env python3
"""Setup script for Todoist Delegator."""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import venv
import webbrowser


def run(cmd, **kwargs):
    print(f"  $ {cmd}")
    result = subprocess.run(cmd, shell=True, **kwargs)
    if result.returncode != 0:
        print(f"  FAILED (exit code {result.returncode})")
        return False
    return True


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


def detect_telegram_chat_id(bot_token):
    """Poll the Telegram bot for a message and extract the chat ID."""
    print("\n  Detecting your chat ID automatically...")
    print("  Send any message to your bot in Telegram now.")
    print("  Waiting", end="", flush=True)

    # Clear any old updates first
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset=-1"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("result"):
                last_id = data["result"][-1]["update_id"]
                # Mark it as read
                urllib.request.urlopen(
                    f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={last_id + 1}",
                    timeout=10,
                )
    except Exception:
        pass

    for _ in range(60):  # Wait up to 60 seconds
        print(".", end="", flush=True)
        time.sleep(1)
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                results = data.get("result", [])
                if results:
                    chat_id = str(results[0]["message"]["chat"]["id"])
                    user = results[0]["message"]["from"]
                    name = user.get("first_name", "")
                    print(f"\n  Found you: {name} (chat ID: {chat_id})")
                    return chat_id
        except Exception:
            continue

    print("\n  Timed out waiting for a message.")
    return None


def configure_env(env_file):
    """Interactively prompt for missing required config values."""
    values = load_env(env_file)

    steps = []
    if not values.get("TODOIST_API_TOKEN"):
        steps.append("TODOIST_API_TOKEN")
    if not values.get("ANTHROPIC_API_KEY"):
        steps.append("ANTHROPIC_API_KEY")
    if not values.get("TELEGRAM_BOT_TOKEN"):
        steps.append("TELEGRAM_BOT_TOKEN")
    if not values.get("TELEGRAM_CHAT_ID"):
        steps.append("TELEGRAM_CHAT_ID")

    if not steps:
        print("[OK] All required config values are set")
        return

    print(f"\n--- Configuration ({len(steps)} value(s) needed) ---\n")
    print("Press Enter to skip any value (you can set it later in .env).\n")

    # --- Todoist ---
    if "TODOIST_API_TOKEN" in steps:
        print("  Opening Todoist developer settings in your browser...")
        webbrowser.open("https://app.todoist.com/app/settings/integrations/developer")
        print("  Copy your API token from the page that just opened.")
        val = input("  Todoist API token: ").strip()
        if val:
            values["TODOIST_API_TOKEN"] = val
            print("  [OK] TODOIST_API_TOKEN set\n")
        else:
            print("  [skipped] You'll need to set TODOIST_API_TOKEN in .env before running.\n")

    # --- Anthropic ---
    if "ANTHROPIC_API_KEY" in steps:
        print("  Opening Anthropic console in your browser...")
        webbrowser.open("https://console.anthropic.com/settings/keys")
        print("  Create an API key and copy it.")
        val = input("  Anthropic API key: ").strip()
        if val:
            values["ANTHROPIC_API_KEY"] = val
            print("  [OK] ANTHROPIC_API_KEY set\n")
        else:
            print("  [skipped] You'll need to set ANTHROPIC_API_KEY in .env before running.\n")

    # --- Telegram bot token ---
    if "TELEGRAM_BOT_TOKEN" in steps:
        print("  Opening BotFather in Telegram...")
        webbrowser.open("https://t.me/BotFather")
        print("  Steps:")
        print("    1. Click 'Start' or send /newbot to @BotFather")
        print("    2. Follow the prompts to name your bot")
        print("    3. Copy the token it gives you")
        val = input("  Telegram bot token: ").strip()
        if val:
            values["TELEGRAM_BOT_TOKEN"] = val
            print("  [OK] TELEGRAM_BOT_TOKEN set\n")
        else:
            print("  [skipped] You'll need to set TELEGRAM_BOT_TOKEN in .env before running.\n")

    # --- Telegram chat ID (auto-detect if we have the bot token) ---
    if "TELEGRAM_CHAT_ID" in steps:
        bot_token = values.get("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            chat_id = detect_telegram_chat_id(bot_token)
            if chat_id:
                values["TELEGRAM_CHAT_ID"] = chat_id
                print("  [OK] TELEGRAM_CHAT_ID set automatically\n")
            else:
                print("  Could not detect chat ID automatically.")
                print("  You can message @userinfobot on Telegram to find your chat ID.")
                val = input("  Telegram chat ID: ").strip()
                if val:
                    values["TELEGRAM_CHAT_ID"] = val
                    print("  [OK] TELEGRAM_CHAT_ID set\n")
                else:
                    print("  [skipped] You'll need to set TELEGRAM_CHAT_ID in .env before running.\n")
        else:
            print("  To get your chat ID:")
            print("    1. Message @userinfobot on Telegram")
            print("    2. It replies with your numeric ID")
            val = input("  Telegram chat ID: ").strip()
            if val:
                values["TELEGRAM_CHAT_ID"] = val
                print("  [OK] TELEGRAM_CHAT_ID set\n")
            else:
                print("  [skipped] You'll need to set TELEGRAM_CHAT_ID in .env before running.\n")

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
    required = ["TODOIST_API_TOKEN", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    still_missing = [key for key in required if not values.get(key)]
    if still_missing:
        print("Before running, set these in .env:")
        for key in still_missing:
            print(f"  - {key}")
        print()

    print("To run manually:")
    if os.name == "nt":
        print("  .venv\\Scripts\\activate")
    else:
        print("  source .venv/bin/activate")
    print("  python -m src.main")
    print()
    print("To run as a background service (macOS):")
    print("  python3 install_service.py")
    print()
    print("To verify your setup:")
    print("  python3 verify_setup.py")
    print()


if __name__ == "__main__":
    main()
