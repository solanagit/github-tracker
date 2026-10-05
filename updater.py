"""Housekeeping shared by run.py and bot.py: settings, logging, one-copy-only, and self-updating."""
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOGS = HERE / "logs"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

log = logging.getLogger("tracker")
_lock_file = None


def load_env():
    """Reads KEY=value lines from .env into the environment (real environment variables win)."""
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def setup_logging():
    if logging.getLogger().handlers:
        return
    LOGS.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    to_file = RotatingFileHandler(LOGS / "bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    to_screen = logging.StreamHandler()
    for handler in (to_file, to_screen):
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[to_file, to_screen])
    logging.getLogger("discord").setLevel(logging.WARNING)
    # Under Task Scheduler nobody sees the console, so make sure crashes reach the log file.
    sys.excepthook = lambda *exc: log.critical("Crashed", exc_info=exc)
    threading.excepthook = lambda args: log.critical(
        "Crashed in a background thread", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def single_instance():
    """True if no other copy of the bot is running. Holds a lock until this process exits."""
    global _lock_file
    DATA.mkdir(exist_ok=True)
    f = open(DATA / "bot.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _lock_file = f
    return True


def _git(*args):
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    # safe.directory: the scheduled task runs as SYSTEM, which doesn't own this folder.
    return subprocess.run([git, "-C", str(HERE), "-c", "safe.directory=*", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, creationflags=NO_WINDOW)


def pull_update():
    """Makes this folder match its GitHub repo. True if the code changed (restart to use it)."""
    if not (HERE / ".git").exists():
        return False
    try:
        fetch = _git("fetch", "--quiet", "origin")
        if fetch.returncode:
            log.warning("Update check failed: %s", fetch.stderr.strip())
            return False
        head = _git("rev-parse", "HEAD").stdout.strip()
        upstream = _git("rev-parse", "@{upstream}").stdout.strip()
        if not upstream or head == upstream:
            return False
        reset = _git("reset", "--quiet", "--hard", "@{upstream}")
        if reset.returncode:
            log.warning("Update failed: %s", reset.stderr.strip())
            return False
    except Exception as e:  # git missing, GitHub unreachable, ...
        log.warning("Update check failed: %s", e)
        return False
    log.info("Updated to version %s", upstream[:7])
    return True


def install_requirements():
    """pip-installs requirements.txt, but only when it changed since the last install."""
    req = HERE / "requirements.txt"
    stamp = DATA / "requirements.installed"
    wanted = hashlib.sha256(req.read_bytes()).hexdigest()
    if stamp.exists() and stamp.read_text().strip() == wanted:
        return
    log.info("Installing requirements")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-r", str(req)],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode:
        log.error("Installing requirements failed: %s", result.stderr.strip()[-2000:])
        return
    DATA.mkdir(exist_ok=True)
    stamp.write_text(wanted)
