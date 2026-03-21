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

    # 7. Create .env from template if missing
    env_file = os.path.join(root, ".env")
    if not os.path.exists(env_file):
        print("\nCreating .env from template...")
        shutil.copy(os.path.join(root, ".env.example"), env_file)
        print("[!!] .env created — you need to fill in your API tokens:")
        print("     TODOIST_API_TOKEN: https://app.todoist.com/app/settings/integrations/developer")
        print("     ANTHROPIC_API_KEY: https://console.anthropic.com/")
    else:
        print("[OK] .env already exists")

    # 8. Create workspace directory
    os.makedirs(os.path.join(root, "agent-workspace"), exist_ok=True)

    print("\n=== Setup Complete ===\n")
    print("Next steps:")
    print("  1. Edit .env and add your API tokens")
    print("  2. Activate the venv:")
    if os.name == "nt":
        print("       .venv\\Scripts\\activate")
    else:
        print("       source .venv/bin/activate")
    print("  3. Run the service:")
    print("       python -m src.main")
    print()


if __name__ == "__main__":
    main()
