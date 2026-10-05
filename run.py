"""Starts GitHub Tracker.

Windows Task Scheduler runs this at boot and every minute after; if the bot is already
running, the extra copy exits straight away. Before starting, it pulls the latest version
from GitHub and installs any new requirements.

This file only uses the standard library, so that even a broken update of updater.py can
be fixed by pushing a new version. A broken run.py itself has to be fixed on the VPS by hand.
"""
import sys


def recover():
    """updater.py failed to load: log why, then pull the latest version so the next start can work."""
    import datetime
    import subprocess
    import traceback
    from pathlib import Path
    here = Path(__file__).resolve().parent
    try:
        (here / "logs").mkdir(exist_ok=True)
        with open(here / "logs" / "bot.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  ERROR    Couldn't load updater.py; "
                    f"pulling the latest version from GitHub\n{traceback.format_exc()}\n")
    except OSError:
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for cmd in (["git", "-C", str(here), "-c", "safe.directory=*", "fetch", "--quiet", "origin"],
                ["git", "-C", str(here), "-c", "safe.directory=*", "reset", "--quiet", "--hard", "@{upstream}"],
                [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(here / "requirements.txt")]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=300, creationflags=flags)
        except Exception:
            pass


def main():
    try:
        import updater
    except Exception:
        recover()
        return 1
    if not updater.single_instance():
        return 0
    updater.setup_logging()
    try:
        updater.pull_update()
        updater.install_requirements()
        import bot  # imported only now, so the code just pulled is what runs
        return bot.main()
    except Exception as e:
        updater.log.error("The bot stopped because of an error: %s: %s; trying again in a minute",
                          type(e).__name__, e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
