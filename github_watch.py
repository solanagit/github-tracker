"""The GitHub side of the tracker: reading links, finding new commits, and formatting them for Discord.

Everything here is plain blocking code; the bot runs it in a worker thread.
"""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("tracker")

FETCH = 30              # commits fetched per check
MAX_LISTED = 10         # commits listed in one Discord message
SAFE_WITHOUT_TOKEN = 7  # GitHub allows 60 requests an hour without a token; each repo uses 6+
USER_AGENT = "github-tracker/2.0"
GREEN = 0x2DA44E
_token_rejected = False  # set when GitHub refuses GITHUB_TOKEN (e.g. it expired); then we go without it


class NotFound(Exception):
    """The repo, branch, commit or account doesn't exist, or is private."""


class RateLimited(Exception):
    """GitHub is refusing requests for now."""


def github_get(path, token="", etag=None):
    """Returns (json, etag), or (None, etag) when nothing changed since `etag`."""
    global _token_rejected
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token and not _token_rejected:
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
        if e.code == 409:  # the repo exists but has no commits yet
            return [], None
        if e.code in (404, 422):
            raise NotFound(path) from None
        if e.code == 401 and "Authorization" in headers:
            _token_rejected = True
            log.error("GitHub rejected GITHUB_TOKEN (expired?). Carrying on without it, which allows fewer "
                      "repos. To fix: run install.ps1 again and paste a new token.")
            return github_get(path, token, etag)
        if e.code in (403, 429):
            raise RateLimited("GitHub rate limit hit; will retry next check") from None
        raise


# ---------- Links ----------

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


def describe(owner, repo, branch):
    """How a tracked thing is shown to people: 'owner/repo', 'owner/repo (dev)' or 'owner (all repos)'."""
    if not repo:
        return f"{owner} (all repos)"
    return f"{owner}/{repo}" + (f" ({branch})" if branch else "")


# ---------- GitHub lookups ----------

def repo_info(owner, repo, token):
    return github_get(f"/repos/{owner}/{repo}", token)[0]


def account_info(owner, token):
    return github_get(f"/users/{owner}", token)[0]


def account_repos(owner, token, etag=None):
    """An account's own public repos (not forks) as [{"id", "name", "archived"}], or None if unchanged.

    Reads every page (up to 1000 repos) whenever the first page has changed. The id stays the
    same when a repo is renamed.
    """
    path = f"/users/{owner}/repos?sort=pushed&per_page=100"
    batch, etag = github_get(path, token, etag)
    if batch is None:
        return None, etag
    listing, page = list(batch), 1
    while len(batch) == 100 and page < 10:
        page += 1
        batch, _ = github_get(f"{path}&page={page}", token)
        listing += batch
    return [{"id": str(r["id"]), "name": r["full_name"], "archived": bool(r["archived"])}
            for r in listing if not r["fork"]], etag


def fetch_commits(full_name, branch, token, etag=None):
    """Newest-first commits of a branch (None: the default branch), or None if unchanged."""
    path = f"/repos/{full_name}/commits?per_page={FETCH}"
    if branch:
        path += "&sha=" + urllib.parse.quote(branch, safe="")
    return github_get(path, token, etag)


def commit_detail(full_name, sha, token):
    """One commit, including `files` with the lines it changed."""
    return github_get(f"/repos/{full_name}/commits/{sha}", token)[0]


def new_since(full_name, commits, last, token):
    """What a branch gained since commit `last`, given its newest commits (newest first).

    Returns a dict:
      new    the new commits, oldest first
      total  how many there are, or None if unknown (more than we fetched)
      note   a remark for people (e.g. after a force-push), or None
      files  the changed files of exactly those commits, or None if not known yet
      url    a GitHub page showing the change
    Uses GitHub's compare, so merged branches and force-pushes are handled correctly.
    """
    head = commits[0]["sha"]
    change = {"new": [], "total": 0, "note": None, "files": None,
              "url": f"https://github.com/{full_name}/commit/{head}"}
    if last is None:
        # The first content we see (an empty repo's first push, or a brand-new repo).
        change.update(new=commits[::-1], total=len(commits) if len(commits) < FETCH else None)
        return change
    try:
        diff, _ = github_get(f"/repos/{full_name}/compare/{last}...{head}", token)
    except NotFound:  # the commit we last saw no longer exists (history was rewritten)
        change.update(new=[commits[0]], total=1,
                      note="History was rewritten (force-push). Showing the newest commit.")
        return change
    if diff.get("status") in ("behind", "identical"):
        return change  # the branch moved back to an older commit; nothing new
    new = diff.get("commits") or []
    change.update(new=new, total=diff.get("total_commits", len(new)), files=diff.get("files"),
                  url=diff.get("html_url") or change["url"],
                  note="History was rewritten (force-push)." if diff.get("status") == "diverged" else None)
    return change


# ---------- Discord embeds (as dicts) ----------

def make_embed(title, description, url=None, color=GREEN, timestamp=None):
    """An embed dict cut to Discord's limits (title 256, description 4096 characters)."""
    e = {"title": title[:256], "description": description[:4096], "color": color}
    if url:
        e["url"] = url
    if timestamp:
        e["timestamp"] = timestamp
    return e


def commit_line(c):
    message = c["commit"]["message"].strip().splitlines()
    title = message[0] if message else "(no message)"
    if len(title) > 100:
        title = title[:97] + "..."
    author = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
    return f"[`{c['sha'][:7]}`]({c['html_url']}) {title} - {author}"


def commits_embed(full_name, branch, new, total, note=None):
    """`new` is oldest-first; `total` is the real number of new commits, or None if unknown."""
    shown = new[-MAX_LISTED:]
    hidden = (total if total is not None else len(new)) - len(shown)
    lines = [commit_line(c) for c in shown]
    if hidden > 0:
        more = "" if total is not None else "+"
        lines.insert(0, f"...plus {hidden}{more} earlier commit{'' if hidden == 1 and not more else 's'}")
    if note:
        lines.insert(0, f"*{note}*")
    count = str(total) if total is not None else f"{len(new)}+"
    owner, repo = full_name.split("/", 1)
    return make_embed(
        f"[{describe(owner, repo, branch)}] {count} new commit{'' if count == '1' else 's'}",
        "\n".join(lines),
        url=f"https://github.com/{full_name}/commits" + (f"/{branch}" if branch else ""),
        timestamp=shown[-1]["commit"]["committer"]["date"],
    )


def new_repo_embed(full_name):
    return make_embed(f"New repo: {full_name}", "Its commits will be posted here too.",
                      url=f"https://github.com/{full_name}")
