#!/usr/bin/env python3
"""Install todoist-delegator as a macOS launchd service."""

import os
import sys

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.todoist-delegator</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>src.main</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{project_dir}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>

    <key>StandardErrorPath</key>
    <string>{log_path}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
"""

LABEL = "com.todoist-delegator"


def main():
    if sys.platform != "darwin":
        print("This script is for macOS only.")
        print("On Linux, consider creating a systemd user service instead.")
        sys.exit(1)

    project_dir = os.path.dirname(os.path.abspath(__file__))
    python_path = os.path.join(project_dir, ".venv", "bin", "python")
    log_path = os.path.join(project_dir, "agent-workspace", "delegator.log")

    if not os.path.exists(python_path):
        print(f"Virtual environment not found at {python_path}")
        print("Run 'python3 setup.py' first to create the virtual environment.")
        sys.exit(1)

    # Ensure log directory exists
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    plist_content = PLIST_TEMPLATE.format(
        python_path=python_path,
        project_dir=project_dir,
        log_path=log_path,
    )

    plist_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(plist_dir, exist_ok=True)
    plist_path = os.path.join(plist_dir, f"{LABEL}.plist")

    # Check if already loaded
    if os.path.exists(plist_path):
        print(f"Plist already exists at {plist_path}")
        reply = input("Overwrite? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            sys.exit(0)
        print(f"  Unloading existing service...")
        os.system(f"launchctl unload {plist_path} 2>/dev/null")

    with open(plist_path, "w") as f:
        f.write(plist_content)

    print(f"Installed plist to {plist_path}")
    print()
    print("To start the service:")
    print(f"  launchctl load {plist_path}")
    print()
    print("To check status:")
    print("  launchctl list | grep todoist")
    print()
    print("To view logs:")
    print(f"  tail -f {log_path}")
    print()
    print("To stop the service:")
    print(f"  launchctl unload {plist_path}")


if __name__ == "__main__":
    main()
