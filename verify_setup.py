#!/usr/bin/env python3
"""Verify that Todoist Delegator is fully configured and ready to run."""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error


ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(ROOT, ".env")

passed = 0
failed = 0
warnings = 0


def ok(msg):
    global passed
    passed += 1
    print(f"  [OK] {msg}")


def fail(msg):
    global failed
    failed += 1
    print(f"  [FAIL] {msg}")


def warn(msg):
    global warnings
    warnings += 1
    print(f"  [WARN] {msg}")


def load_env():
    values = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    values[key.strip()] = val.strip()
    return values


def check_prerequisites():
    print("\n1. Prerequisites\n")

    if sys.version_info >= (3, 10):
        ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        fail(f"Python 3.10+ required, you have {sys.version_info.major}.{sys.version_info.minor}")

    if shutil.which("node"):
        ok("Node.js installed")
    else:
        fail("Node.js not found — install from https://nodejs.org/")

    if shutil.which("claude"):
        ok("Claude Code CLI installed")
    else:
        fail("Claude Code CLI not found — install with: npm install -g @anthropic-ai/claude-code")

    if shutil.which("agent-browser"):
        ok("agent-browser installed")
    else:
        warn("agent-browser not found — needed for web_form tasks. Install with: npm install -g agent-browser")

    venv_python = os.path.join(ROOT, ".venv", "bin", "python")
    if os.path.exists(venv_python):
        ok("Virtual environment exists")
    else:
        fail("Virtual environment not found — run: python3 setup.py")


def check_env_file():
    print("\n2. Environment file\n")

    if not os.path.exists(ENV_FILE):
        fail(".env file not found — run: python3 setup.py")
        return {}

    ok(".env file exists")
    values = load_env()

    required = {
        "TODOIST_API_TOKEN": "Get from https://app.todoist.com/app/settings/integrations/developer",
        "ANTHROPIC_API_KEY": "Get from https://console.anthropic.com/settings/keys",
        "TELEGRAM_BOT_TOKEN": "Create a bot via @BotFather on Telegram",
        "TELEGRAM_CHAT_ID": "Run setup.py again to auto-detect, or message @userinfobot",
    }

    for key, help_text in required.items():
        if values.get(key):
            ok(f"{key} is set")
        else:
            fail(f"{key} is missing — {help_text}")

    return values


def check_todoist(token):
    print("\n3. Todoist API\n")

    if not token:
        fail("Skipped — no API token")
        return

    try:
        req = urllib.request.Request(
            "https://api.todoist.com/rest/v2/labels",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            labels = json.loads(resp.read())
            ok("API token is valid")

            # Check for the delegate label
            env = load_env()
            label_name = env.get("DELEGATE_LABEL_NAME", "delegate")
            label_names = [l.get("name", "") for l in labels]
            if label_name in label_names:
                ok(f"Label '{label_name}' exists in Todoist")
            else:
                fail(f"Label '{label_name}' not found in Todoist — create it in your Todoist app")
                if label_names:
                    print(f"         Your labels: {', '.join(label_names)}")

    except urllib.error.HTTPError as e:
        if e.code == 401:
            fail("Todoist API token is invalid (401 Unauthorized)")
        else:
            fail(f"Todoist API error: {e.code}")
    except Exception as e:
        fail(f"Could not reach Todoist API: {e}")


def check_anthropic(key):
    print("\n4. Anthropic API\n")

    if not key:
        fail("Skipped — no API key")
        return

    try:
        body = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok("API key is valid")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fail("Anthropic API key is invalid (401 Unauthorized)")
        elif e.code == 400:
            # A 400 means the key authenticated but the request was bad — still valid
            ok("API key is valid")
        elif e.code == 529:
            warn("Anthropic API is overloaded (529) — key may be valid, try again later")
        else:
            body = e.read().decode() if hasattr(e, 'read') else ""
            fail(f"Anthropic API error {e.code}: {body[:200]}")
    except Exception as e:
        fail(f"Could not reach Anthropic API: {e}")


def check_telegram(bot_token, chat_id):
    print("\n5. Telegram Bot\n")

    if not bot_token:
        fail("Skipped — no bot token")
        return

    # Verify bot token
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{bot_token}/getMe")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            bot_name = data["result"].get("username", "unknown")
            ok(f"Bot token is valid (@{bot_name})")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            fail("Telegram bot token is invalid (401 Unauthorized)")
        else:
            fail(f"Telegram API error: {e.code}")
        return
    except Exception as e:
        fail(f"Could not reach Telegram API: {e}")
        return

    # Verify chat ID by sending a test message
    if not chat_id:
        fail("Skipped chat ID check — no chat ID set")
        return

    try:
        body = json.dumps({
            "chat_id": chat_id,
            "text": "Todoist Delegator setup verified. This bot is working!",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=body,
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok(f"Chat ID is valid — sent a test message to Telegram")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ""
        if "chat not found" in error_body.lower():
            fail(f"Chat ID {chat_id} not found — make sure you've messaged the bot first")
        else:
            fail(f"Could not send to chat {chat_id}: {error_body[:200]}")
    except Exception as e:
        fail(f"Could not send test message: {e}")


def check_service():
    print("\n6. Background service (macOS)\n")

    if sys.platform != "darwin":
        warn("Not macOS — launchd service check skipped")
        return

    plist_path = os.path.expanduser("~/Library/LaunchAgents/com.todoist-delegator.plist")
    if os.path.exists(plist_path):
        ok("launchd plist installed")

        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True,
        )
        if "com.todoist-delegator" in result.stdout:
            ok("Service is loaded and running")
        else:
            warn(f"Service is not loaded — run: launchctl load {plist_path}")
    else:
        warn("launchd plist not installed — run: python3 install_service.py")


def main():
    print("=== Todoist Delegator — Setup Verification ===")

    check_prerequisites()
    values = check_env_file()
    check_todoist(values.get("TODOIST_API_TOKEN", ""))
    check_anthropic(values.get("ANTHROPIC_API_KEY", ""))
    check_telegram(values.get("TELEGRAM_BOT_TOKEN", ""), values.get("TELEGRAM_CHAT_ID", ""))
    check_service()

    print(f"\n{'=' * 45}")
    print(f"  {passed} passed, {failed} failed, {warnings} warnings")
    print(f"{'=' * 45}\n")

    if failed:
        print("Fix the failures above, then run this again.")
        sys.exit(1)
    elif warnings:
        print("All critical checks passed. Warnings are optional.")
    else:
        print("Everything looks good! You're ready to go.")


if __name__ == "__main__":
    main()
