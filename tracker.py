"""GitHub Tracker: posts new commits from GitHub repos to a Discord channel.

What to track is listed in repos.txt, one GitHub link per line. Secrets come from
environment variables, or from a .env file next to this script:

    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    GITHUB_TOKEN=          (optional, lets you track more than about 9 repos)

    python tracker.py             check every tracked repo and post new commits
    python tracker.py --update    first pull the latest version of this folder from GitHub
    python tracker.py --dry-run   print what would be posted, change nothing
    python tracker.py list        show what repos.txt resolves to
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOS_FILE = HERE / "repos.txt"
ENV_FILE = HERE / ".env"
STATE = HERE / "state.json"
LOG = HERE / "tracker.log"

FETCH = 30              # commits fetched from GitHub per check
MAX_LISTED = 10         # commits listed in one Discord message
SAFE_WITHOUT_TOKEN = 9  # GitHub allows 60 anonymous requests an hour; each repo uses 6
USER_AGENT = "github-tracker/1.0"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    if LOG.exists() and LOG.stat().st_size > 500_000:
        LOG.replace(LOG.with_suffix(".old.log"))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def self_update():
    """Makes this folder match its GitHub repo. New code takes effect from the next run."""
    git = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"
    if not (HERE / ".git").exists() or not Path(git).exists():
        log("auto-update skipped: not a git clone, or git isn't installed")
        return
    # safe.directory: the scheduled task runs as SYSTEM, which doesn't own this folder.
    base = [git, "-C", str(HERE), "-c", "safe.directory=*"]

    def git_run(*args):
        return subprocess.run(base + list(args), capture_output=True, text=True, timeout=120)

    before = git_run("rev-parse", "HEAD").stdout.strip()
    for step in (("fetch", "--quiet", "origin"), ("reset", "--quiet", "--hard", "@{upstream}")):
        result = git_run(*step)
        if result.returncode:
            log(f"auto-update failed ({step[0]}): {result.stderr.strip()}")
            return
    after = git_run("rev-parse", "HEAD").stdout.strip()
    if after != before:
        log(f"auto-update: now on {after[:7]}")


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


# ---------- GitHub & Discord ----------

def github_get(path, token, etag=None):
    """Returns (json, etag), or (None, etag) when nothing changed since `etag`."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request("https://api.github.com" + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp), resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        if e.code == 409:  # repo exists but has no commits yet
            return [], None
        if e.code == 404:
            raise LookupError("not found on GitHub (private, renamed or deleted?)") from None
        if e.code in (403, 429):
            raise RuntimeError("GitHub rate limit hit, will retry next run") from None
        raise


def post_to_discord(webhook_url, payload):
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            raise LookupError("Discord rejected the webhook URL (deleted, or copied wrong?)") from None
        raise


# ---------- What to track ----------

def parse_link(text):
    """A github.com link, 'owner/repo' or 'owner' -> (owner, repo or None, branch or None)."""
    t = text.strip().strip("<>\"'").split("?")[0].split("#")[0].rstrip("/")
    t = re.sub(r"^(https?://)?(www\.)?github\.com/", "", t, flags=re.I)
    parts = t.split("/")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", parts[0]):
        raise ValueError(f"not a GitHub link: {text}")
    repo = parts[1] if len(parts) > 1 and parts[1] else None
    if repo and repo.endswith(".git"):
        repo = repo[:-4]
    branch = "/".join(parts[3:]) if len(parts) > 3 and parts[2] == "tree" else None
    return parts[0], repo, branch


def read_targets(token, state):
    """Reads repos.txt into [(repo, branch or None)], expanding account links into their repos."""
    accounts = state.setdefault("_accounts", {})
    targets, seen = [], set()

    def add(repo, branch):
        if (repo.lower(), branch) not in seen:
            seen.add((repo.lower(), branch))
            targets.append((repo, branch))

    for line in REPOS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            owner, repo, branch = parse_link(line)
            if repo:
                add(f"{owner}/{repo}", branch)
                continue
            # A whole account: list its repos (re-checked every run, so new repos get picked up).
            cached = accounts.get(owner.lower(), {})
            try:
                listing, etag = github_get(
                    f"/users/{owner}/repos?type=owner&sort=pushed&per_page=100", token, cached.get("etag"))
                if listing is not None:
                    cached = {"etag": etag, "repos": [
                        r["full_name"] for r in listing if not r["fork"] and not r["archived"]]}
                    accounts[owner.lower()] = cached
            except Exception as e:
                log(f"repos.txt line '{line}': {e} (using the last known list of repos)")
            for name in cached.get("repos", []):
                add(name, None)
        except Exception as e:
            log(f"repos.txt line '{line}': {e}")
    return targets


# ---------- Discord messages ----------

def label(repo, branch):
    return f"{repo}:{branch}" if branch else repo


def commits_url(repo, branch):
    return f"https://github.com/{repo}/commits" + (f"/{branch}" if branch else "")


def commit_line(c):
    message = c["commit"]["message"].strip().splitlines()
    title = message[0] if message else "(no message)"
    if len(title) > 100:
        title = title[:97] + "..."
    author = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
    return f"[`{c['sha'][:7]}`]({c['html_url']}) {title} - {author}"


def commits_message(repo, branch, new, complete):
    """`new` is oldest-first. `complete` is False when older commits were cut off."""
    shown = new[-MAX_LISTED:]
    hidden = len(new) - len(shown)
    lines = [commit_line(c) for c in shown]
    if hidden or not complete:
        lines.insert(0, f"...plus {hidden or 'some'}{'' if complete else '+'} earlier commits")
    count = f"{len(new)}{'' if complete else '+'}"
    return {
        "username": "GitHub Tracker",
        "embeds": [{
            "title": f"[{label(repo, branch)}] {count} new commit{'' if count == '1' else 's'}",
            "url": commits_url(repo, branch),
            "description": "\n".join(lines),
            "color": 0x2DA44E,
            "timestamp": shown[-1]["commit"]["committer"]["date"],
        }],
    }


def welcome_message(started):
    """`started` is a list of (repo, branch, latest commit or None)."""
    lines = []
    for repo, branch, latest in started:
        lines.append(f"**[{label(repo, branch)}](https://github.com/{repo})**")
        lines.append("latest: " + commit_line(latest) if latest else "no commits yet")
    return {
        "username": "GitHub Tracker",
        "embeds": [{
            "title": "Now tracking " + (label(*started[0][:2]) if len(started) == 1 else f"{len(started)} repos"),
            "description": "\n".join(lines)[:4000] + "\n\nNew commits will be posted here.",
            "color": 0x2DA44E,
        }],
    }


# ---------- Checking for new commits ----------

def check(repo, branch, token, seen):
    """Returns (kind, value, new_state): kind is "welcome", "commits", "same" or "unchanged"."""
    path = f"/repos/{repo}/commits?per_page={FETCH}" + (f"&sha={branch}" if branch else "")
    commits, etag = github_get(path, token, seen and seen.get("etag"))
    if commits is None:
        return "unchanged", None, seen
    latest = commits[0] if commits else None
    new_state = {"last_sha": latest and latest["sha"], "etag": etag}

    if seen is None:
        return "welcome", latest, new_state
    if not commits or latest["sha"] == seen["last_sha"]:
        return "same", None, new_state
    shas = [c["sha"] for c in commits]
    found = seen["last_sha"] in shas
    new = commits[: shas.index(seen["last_sha"])] if found else commits
    # Fewer than FETCH commits means we have the whole history, so the count is exact either way.
    complete = found or len(commits) < FETCH
    return "commits", commits_message(repo, branch, list(reversed(new)), complete), new_state


def run(dry_run):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not dry_run and not webhook_url.startswith("https://"):
        log("DISCORD_WEBHOOK_URL isn't set (put it in .env)")
        return 1
    token = os.environ.get("GITHUB_TOKEN", "")

    state = load_state()
    targets = read_targets(token, state)
    started, failed = [], False

    def deliver(payload, updates, note):
        # Progress is saved only after Discord accepts the message, so a failed post is retried next run.
        if dry_run:
            print(json.dumps(payload, indent=2))
        else:
            post_to_discord(webhook_url, payload)
            state.update(updates)
            log(note)

    for repo, branch in targets:
        key = f"{repo.lower()}@{branch or ''}"
        try:
            kind, value, new_state = check(repo, branch, token, state.get(key))
            if kind == "welcome":
                started.append((repo, branch, value, key, new_state))
            elif kind == "commits":
                deliver(value, {key: new_state}, f"{label(repo, branch)}: posted new commits")
            else:
                state[key] = new_state
        except Exception as e:  # keep going with the other repos; retried next run
            failed = True
            log(f"{label(repo, branch)}: {e}")

    if started:
        try:
            deliver(welcome_message([s[:3] for s in started]), {s[3]: s[4] for s in started},
                    "now tracking: " + ", ".join(label(*s[:2]) for s in started))
        except Exception as e:
            failed = True
            log(f"welcome message: {e}")

    if not dry_run:
        STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return 1 if failed else 0


def show_list():
    targets = read_targets(os.environ.get("GITHUB_TOKEN", ""), {})
    for repo, branch in targets:
        print(f"  {label(repo, branch)}")
    if not targets:
        print("repos.txt has nothing to track yet.")
    if len(targets) > SAFE_WITHOUT_TOKEN and not os.environ.get("GITHUB_TOKEN"):
        print(f"\nThat's {len(targets)} repos. Without a GITHUB_TOKEN, GitHub only allows about "
              f"{SAFE_WITHOUT_TOKEN} at a 10-minute interval.")


def main():
    args = sys.argv[1:]
    if "--update" in args:
        self_update()  # before reading repos.txt and .env, so edits on GitHub apply right away
    load_env()
    if args == ["list"]:
        show_list()
    elif set(args) <= {"--update", "--dry-run"}:
        sys.exit(run(dry_run="--dry-run" in args))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
