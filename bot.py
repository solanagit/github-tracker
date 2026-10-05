"""GitHub Tracker: a Discord bot that posts new GitHub commits and flags crypto in them.

Slash commands. /add and /remove need the Manage Server permission unless an admin changes
that in Server Settings > Integrations:
    /add <link> [channel]   track a repo, a branch, or every public repo of an account
    /remove <what>          stop tracking it
    /list                   show what this server tracks

Every POLL_MINUTES the bot checks for new commits. When they mention a $TICKER or add a wallet
or contract address, a highlighted alert is posted with them (see detect.py).
"""
import asyncio
import json
import logging
import os
import secrets
import sys
from typing import Optional

import aiohttp
import discord
from discord import app_commands

import detect
import github_watch as gh
import updater

log = logging.getLogger("tracker")

STORE = updater.DATA / "tracked.json"
STATUS = updater.DATA / "status.txt"  # install.ps1 waits for this to know the bot came online
EXIT_UPDATED = 3
EXIT_BAD_TOKEN = 2
MESSAGE_LIMIT = 6000  # Discord's limit for all embeds in one message together
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)


def target_key(full_name, branch):
    return f"{full_name.lower()}@{branch or ''}"


def same_thing(sub, owner, repo, branch):
    return (sub["owner"].lower() == owner.lower() and (sub["repo"] or "").lower() == (repo or "").lower()
            and sub["branch"] == branch)


def sub_label(sub):
    return gh.describe(sub["owner"], sub["repo"], sub["branch"])


def embed_size(embed):
    return len(discord.Embed.from_dict(embed))


PING_ON = [app_commands.Choice(name="Every update", value="all"),
           app_commands.Choice(name="Only crypto alerts", value="crypto")]


def pings(subs, crypto):
    """(message text, allowed mentions) to ping the roles these subscriptions ask for, or (None, None).

    `crypto` says whether the post includes a crypto alert. A ping role equal to the server's
    id is @everyone.
    """
    roles = sorted({(s["guild"], s["ping"]) for s in subs
                    if s.get("ping") and (s.get("ping_on", "all") == "all" or crypto)})
    if not roles:
        return None, None
    everyone = any(role == guild for guild, role in roles)
    text = " ".join("@everyone" if role == guild else f"<@&{role}>" for guild, role in roles)
    return text, discord.AllowedMentions(everyone=everyone, users=False, replied_user=False,
                                         roles=[discord.Object(role) for guild, role in roles if role != guild])


def ping_label(sub):
    if not sub.get("ping"):
        return ""
    who = "@everyone" if sub["ping"] == sub["guild"] else f"<@&{sub['ping']}>"
    return f" - pings {who}" + (" on crypto alerts" if sub.get("ping_on") == "crypto" else "")


def ping_not_allowed(member, role, channel):
    """Why `member` may not make the bot ping `role` in `channel`, or None if they may.

    Anyone can ping a role that allows @mentions. For @everyone or a locked role, the person
    setting it up needs that permission themselves, so the bot can't be used to get around it.
    """
    if role.is_default() or not role.mentionable:
        if not channel.permissions_for(member).mention_everyone:
            name = "@everyone" if role.is_default() else f"@{role.name}"
            return (f"You need the **Mention @everyone, @here and All Roles** permission in {channel.mention} "
                    f"to make me ping **{name}**.")
    return None


def ping_problem(role, channel):
    """Why the bot couldn't ping `role` in `channel`, or None if it can."""
    if channel.permissions_for(channel.guild.me).mention_everyone:
        return None  # allowed to ping any role
    if role.is_default():
        return ("To ping @everyone I need the **Mention @everyone, @here and All Roles** permission "
                "in that channel.")
    if not role.mentionable:
        return (f"I can't ping **@{role.name}** yet. In **Server Settings → Roles → {role.name}**, "
                "turn on **Allow anyone to @mention this role**.")
    return None


class Store:
    """What the bot remembers, saved to data/tracked.json.

    subscriptions: what each channel tracks.
    targets:  per repo+branch, the last commit posted to each channel, plus a cache tag.
    accounts: per tracked account, its repos by GitHub id (to notice new and renamed ones)
              and the names of the active (not archived) ones.
    seen:     per repo and channel, the crypto already reported there.
    """

    def __init__(self, path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            broken = path.with_name(path.name + ".broken")
            path.replace(broken)
            log.error("%s was damaged; moved it to %s and started with an empty list", path.name, broken.name)
            data = {}
        self.subs = data.get("subscriptions", [])
        self.targets = data.get("targets", {})
        self.accounts = data.get("accounts", {})
        self.seen = data.get("seen", {})

    def save(self):
        self.path.parent.mkdir(exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"subscriptions": self.subs, "targets": self.targets,
                                   "accounts": self.accounts, "seen": self.seen}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # all-or-nothing, so a crash can't leave half a file

    def guild_subs(self, guild_id):
        return [s for s in self.subs if s["guild"] == guild_id]

    def drop_subs(self, should_drop, why):
        """Removes matching subscriptions. No await inside, so it's safe alongside a running check."""
        gone = [s for s in self.subs if should_drop(s)]
        if gone:
            self.subs = [s for s in self.subs if not should_drop(s)]
            self.save()
            log.info("%s; stopped tracking %s", why, ", ".join(sub_label(s) for s in gone))
        return gone

    def account_repo_names(self, owner):
        return self.accounts.get(owner.lower(), {}).get("repos", [])

    def repo_count(self):
        names = {target_key(f"{s['owner']}/{s['repo']}", s["branch"]) for s in self.subs if s["repo"]}
        for s in self.subs:
            if not s["repo"]:
                names |= {target_key(n, None) for n in self.account_repo_names(s["owner"])}
        return len(names)

    def covering_subs(self, channel, full_name, branch):
        """The subscriptions through which `channel` (an id string) gets this repo+branch."""
        key = target_key(full_name, branch)
        found = []
        for s in self.subs:
            if str(s["channel"]) != channel:
                continue
            if s["repo"] and target_key(f"{s['owner']}/{s['repo']}", s["branch"]) == key:
                found.append(s)
            elif not s["repo"] and branch is None and any(
                    target_key(n, None) == key for n in self.account_repo_names(s["owner"])):
                found.append(s)
        return found

    def tracks(self, channel, full_name, branch):
        """Whether `channel` (an id string) still tracks this repo+branch, directly or via its account."""
        return bool(self.covering_subs(channel, full_name, branch))

    def account_subs_in(self, channel, owner):
        return [s for s in self.subs
                if not s["repo"] and s["owner"].lower() == owner.lower() and str(s["channel"]) == channel]

    @staticmethod
    def account_entry(listing, etag):
        return {"etag": etag, "ids": {r["id"]: r["name"] for r in listing},
                "repos": [r["name"] for r in listing if not r["archived"]]}


class Tree(app_commands.CommandTree):
    async def on_error(self, interaction, error):
        name = interaction.command.name if interaction.command else "?"
        log.error("/%s failed", name, exc_info=error)
        msg = "Sorry, something went wrong. The details are in the bot's log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class TrackerBot(discord.Client):
    def __init__(self, github_token="", poll_minutes=10):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = Tree(self)
        self.github_token = github_token
        self.poll_minutes = poll_minutes
        self.store = Store(STORE)
        self.lock = asyncio.Lock()  # one GitHub check or /add at a time
        self.restart_for_update = False
        self.synced_guilds = set()
        self.alerted_now = {}
        add_commands(self)

    # ---------- Discord events ----------

    async def setup_hook(self):
        self.poller = asyncio.create_task(self.poll_forever())

    async def on_ready(self):
        log.info("Logged in as %s, in %d server(s)", self.user, len(self.guilds))
        STATUS.write_text(f"online as {self.user}\n", encoding="utf-8")
        # Catch up on servers and channels removed while the bot was offline.
        guilds = {g.id: g for g in self.guilds}

        def gone(s):
            g = guilds.get(s["guild"])
            return g is None or (not g.unavailable and g.get_channel(s["channel"]) is None)
        self.store.drop_subs(gone, "Server or channel no longer exists")
        # ...and roles deleted meanwhile: stop pinging them.
        stale = [s for s in self.store.subs if s.get("ping") and s["ping"] != s["guild"]
                 and (g := guilds.get(s["guild"])) and not g.unavailable and g.get_role(s["ping"]) is None]
        for s in stale:
            s["ping"], s["ping_on"] = None, "all"
        if stale:
            self.store.save()
            log.info("Stopped pinging %d deleted role(s)", len(stale))
        for guild in self.guilds:
            await self.sync_commands(guild)
        if not self.guilds:
            log.info("Not in any server yet. Add me with: %s", self.invite_url())

    async def on_guild_join(self, guild):
        log.info("Joined server %s", guild.name)
        await self.sync_commands(guild)

    async def on_guild_remove(self, guild):
        self.synced_guilds.discard(guild.id)
        self.store.drop_subs(lambda s: s["guild"] == guild.id, f"Removed from server {guild.name}")

    async def on_guild_role_delete(self, role):
        changed = [s for s in self.store.subs if s.get("ping") == role.id]
        for s in changed:
            s["ping"], s["ping_on"] = None, "all"
        if changed:
            self.store.save()
            log.info("Role %s was deleted; stopped pinging it", role.name)

    async def on_guild_channel_delete(self, channel):
        self.store.drop_subs(lambda s: s["channel"] == channel.id, f"Channel #{channel.name} was deleted")

    def invite_url(self):
        return discord.utils.oauth_url(self.application_id, permissions=INVITE_PERMISSIONS,
                                       scopes=("bot", "applications.commands"))

    async def sync_commands(self, guild):
        """Registers the slash commands in a server (per server, so they show up immediately)."""
        if guild.id in self.synced_guilds:
            return
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.synced_guilds.add(guild.id)
        except discord.Forbidden:
            log.warning("Can't add slash commands in %s. Re-invite me with: %s", guild.name, self.invite_url())
        except Exception as e:
            log.warning("Couldn't add slash commands in %s yet (%s); will retry", guild.name, e)

    # ---------- Checking GitHub ----------

    async def poll_forever(self):
        await self.wait_until_ready()
        while not self.is_closed():
            for guild in self.guilds:  # retries any server whose commands failed to register
                await self.sync_commands(guild)
            try:
                await self.poll()
            except Exception:
                log.exception("Checking GitHub failed")
            try:
                updated = await asyncio.to_thread(updater.pull_update)
            except Exception:
                log.exception("Update check failed")
                updated = False
            if updated:
                log.info("Restarting to use the new version")
                self.restart_for_update = True
                await self.close()
                return
            await asyncio.sleep(self.poll_minutes * 60)

    async def poll(self):
        async with self.lock:
            store = self.store
            self.alerted_now = {}  # per repo and channel: crypto alerted during this check
            # 1. Refresh tracked accounts' repo lists, announcing repos created since last time.
            account_subs = {}
            for sub in store.subs:
                if not sub["repo"]:
                    account_subs.setdefault(sub["owner"].lower(), []).append(sub)
            for key, subs in account_subs.items():
                cached = store.accounts.get(key, {})
                try:
                    listing, etag = await asyncio.to_thread(
                        gh.account_repos, subs[0]["owner"], self.github_token, cached.get("etag"))
                except Exception as e:
                    log.warning("Listing %s's repos failed: %s", subs[0]["owner"], e)
                    continue
                if listing is None:
                    continue
                if "ids" in cached:
                    await self.account_changes(subs[0]["owner"], cached["ids"], listing,
                                               {str(s["channel"]) for s in subs})
                store.accounts[key] = Store.account_entry(listing, etag)
            for key in list(store.accounts):
                if key not in account_subs:
                    del store.accounts[key]

            # 2. Which repo+branch goes to which channels.
            wanted = {}
            for sub in store.subs:
                if sub["repo"]:
                    pairs = [(f"{sub['owner']}/{sub['repo']}", sub["branch"])]
                else:
                    pairs = [(n, None) for n in store.accounts.get(sub["owner"].lower(), {}).get("repos", [])]
                for name, branch in pairs:
                    w = wanted.setdefault(target_key(name, branch), {"name": name, "branch": branch, "channels": set()})
                    w["channels"].add(str(sub["channel"]))
            # Keep the state of archived repos in tracked accounts: if one is unarchived, it carries on
            # from where it was instead of skipping whatever is pushed right after.
            account_repos = {target_key(n, None) for a in store.accounts.values() for n in a.get("ids", {}).values()}
            for key in list(store.targets):
                if key not in wanted and key not in account_repos:
                    del store.targets[key]
            channels_in_use = {str(s["channel"]) for s in store.subs}
            for key in list(store.seen):
                if key.rsplit("#", 1)[-1] not in channels_in_use:
                    del store.seen[key]
            store.save()

            # 3. Check each repo once and post to every channel that tracks it.
            for key, w in wanted.items():
                try:
                    await self.check_target(key, w)
                except Exception as e:
                    log.warning("%s: %s", w["name"], e)
                store.save()

    async def account_changes(self, owner, known_ids, listing, channels):
        """Handles repos that appeared in a tracked account's listing since the last check."""
        store = self.store
        # Renames first, so a new repo that takes over an old name doesn't inherit the old repo's state.
        for r in listing:
            old_name = known_ids.get(r["id"])
            if old_name is not None and old_name != r["name"]:
                self.rename_repo(old_name, r["name"])
        for r in listing:
            if r["id"] in known_ids or r["archived"]:
                continue
            # Created, made public or moved into the account. Start from its very first commit,
            # so its first content is posted and scanned (that's where a launch shows up).
            t = store.targets[target_key(r["name"], None)] = {"etag": None, "channels": {}}
            for channel in sorted(channels):
                subs = store.account_subs_in(channel, owner)
                if subs:  # not /removed in the meantime
                    t["channels"][channel] = None
                    await self.deliver(int(channel), [gh.new_repo_embed(r["name"])], *pings(subs, crypto=False))

    def rename_repo(self, old_name, new_name):
        """A tracked repo was renamed: move everything to the new name, so nothing is repeated or lost."""
        store, old, new = self.store, old_name.lower(), new_name.lower()
        new_owner, new_repo = new_name.split("/", 1)
        for s in store.subs:  # subscriptions to the repo itself
            if s["repo"] and f"{s['owner']}/{s['repo']}".lower() == old:
                s["owner"], s["repo"] = new_owner, new_repo
        for key in [k for k in store.targets if k.rsplit("@", 1)[0] == old]:  # every tracked branch
            store.targets[new + "@" + key.rsplit("@", 1)[1]] = store.targets.pop(key)
        for key in [k for k in store.seen if k.rsplit("#", 1)[0] == old]:
            store.seen[new + "#" + key.rsplit("#", 1)[1]] = store.seen.pop(key)
        log.info("%s was renamed to %s", old_name, new_name)

    async def check_target(self, key, w):
        t = self.store.targets.setdefault(key, {"etag": None, "channels": {}})
        for channel in list(t["channels"]):
            if channel not in w["channels"]:
                del t["channels"][channel]
        fresh = {c for c in w["channels"] if c not in t["channels"]}
        followed = [v for c, v in t["channels"].items() if c not in fresh]  # where other channels are
        commits, etag = await asyncio.to_thread(
            gh.fetch_commits, w["name"], w["branch"], self.github_token, None if fresh else t["etag"])
        if commits is None:
            return  # nothing changed since last time
        head = commits[0]["sha"] if commits else None

        delivered_all = True
        changes, scans = {}, {}  # per previous commit: what's new, and the crypto in it
        for channel in sorted(w["channels"]):
            if channel in fresh:
                # Just started tracking here. If another channel already follows this repo, catch up
                # from where that one is; otherwise start from the current commit.
                t["channels"][channel] = followed[0] if followed else head
                if not followed:
                    continue
            last = t["channels"][channel]
            if not commits or last == head:
                continue
            if last not in changes:
                changes[last] = await asyncio.to_thread(gh.new_since, w["name"], commits, last, self.github_token)
            change = changes[last]
            if not change["new"]:  # the branch moved back to an older commit
                t["channels"][channel] = head
                continue
            if last not in scans:
                scans[last] = await asyncio.to_thread(
                    detect.find_crypto, w["name"], change["new"], change["files"], self.github_token)
            commits_post = gh.commits_embed(w["name"], w["branch"], change["new"], change["total"], change["note"])
            seen_key = f"{w['name'].lower()}#{channel}"
            seen = self.store.seen.get(seen_key, [])
            alert, shown, leftover = await asyncio.to_thread(
                detect.build_alert, w["name"], scans[last], set(seen), change["url"],
                MESSAGE_LIMIT - embed_size(commits_post) - 50)
            subs = self.store.covering_subs(channel, w["name"], w["branch"])
            if not subs:
                continue  # /removed while this check was running
            # Crypto-only pings go out when this post brings crypto: in its alert, in the follow-up,
            # or crypto another branch of this repo alerted about in this channel earlier in this check.
            crypto = bool(alert or leftover or set(scans[last]) & self.alerted_now.get(seen_key, set()))
            if await self.deliver(int(channel), [commits_post] + ([alert] if alert else []), *pings(subs, crypto)):
                t["channels"][channel] = head
                self.alerted_now.setdefault(seen_key, set()).update(shown, (f["key"] for f in leftover))
                if leftover:  # what didn't fit goes in one short follow-up
                    extra, keys = detect.leftover_embed(w["name"], leftover, change["url"])
                    if await self.deliver(int(channel), [extra]):
                        shown = shown + keys
                if shown:
                    self.store.seen[seen_key] = (seen + shown)[-1000:]
            else:
                delivered_all = False
        t["etag"] = etag if delivered_all else None  # a failed post is retried next time

    async def deliver(self, channel_id, embeds, content=None, mentions=None):
        """Posts embeds (dicts), with optional text that pings the roles `mentions` allows.

        False means try again later; True means done, or impossible.
        """
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            await channel.send(content=content, embeds=[discord.Embed.from_dict(e) for e in embeds],
                               allowed_mentions=mentions or discord.AllowedMentions.none())
            return True
        except (discord.Forbidden, discord.NotFound) as e:
            if isinstance(e, discord.NotFound) and e.code == 10003:  # Unknown Channel
                self.store.drop_subs(lambda s: s["channel"] == channel_id, f"Channel {channel_id} no longer exists")
            log.warning("Can't post in channel %s (%s); skipping", channel_id, e)
            return True
        except discord.HTTPException as e:
            if 400 <= e.status < 500 and e.status != 429:  # Discord refuses this message: retrying won't help
                log.error("Discord refused a post for channel %s (%s); skipping it", channel_id, e)
                return True
            log.warning("Posting in channel %s failed, will retry: %s", channel_id, e)
            return False
        except Exception as e:
            log.warning("Posting in channel %s failed, will retry: %s", channel_id, e)
            return False

    # ---------- Slash commands ----------

    async def add_tracking(self, interaction, link, channel, role=None, ping_on="all"):
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "Use /add in a normal text channel, or pick one with the `channel` option.", ephemeral=True)
            return
        perms = target.permissions_for(target.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I can't post in {target.mention}. Give me **View Channel**, **Send Messages** and "
                "**Embed Links** there, then try again.", ephemeral=True)
            return
        try:
            owner, repo, branch = gh.parse_link(link)
        except ValueError:
            await interaction.response.send_message(
                "That doesn't look like a GitHub link. Try something like `https://github.com/owner/repo`.",
                ephemeral=True)
            return
        refused = role and ping_not_allowed(interaction.user, role, target)
        if refused:
            await interaction.response.send_message(refused, ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        async with self.lock:
            try:
                if repo:
                    result = await self.start_repo(interaction, target, owner, repo, branch)
                else:
                    result = await self.start_account(interaction, target, owner)
            except gh.NotFound:
                what = "branch" if branch else "repo" if repo else "account"
                await interaction.followup.send(f"Couldn't find that {what} on GitHub. Is it public, and is the link right?")
                return
            except gh.RateLimited:
                await interaction.followup.send("GitHub is limiting requests right now. Try again in a few minutes.")
                return
            if isinstance(result, str):  # already tracked
                await interaction.followup.send(result)
                return
            sub, embed = result
            if role:
                sub["ping"], sub["ping_on"] = role.id, ping_on
                embed["description"] = (embed["description"] + "\n\n" + self.ping_note(sub, role, target))[:4096]
            self.store.subs.append(sub)
            self.store.save()
            count = self.store.repo_count()
        if count > gh.SAFE_WITHOUT_TOKEN and not self.github_token:
            embed["description"] = (embed["description"] + f"\n\n**Note:** {count} repos are now tracked. Without a "
                                    f"GitHub token, GitHub only allows about {gh.SAFE_WITHOUT_TOKEN}; see the README.")[:4096]
        await interaction.followup.send(embed=discord.Embed.from_dict(embed))
        log.info("%s added %s in #%s", interaction.user, sub_label(sub), target.name)
        if not repo:
            # Check the account's repos now, so its starting points are set right away and
            # nothing pushed before the next scheduled check is missed.
            self.first_check = asyncio.create_task(self.poll())

    def already_tracked(self, interaction, channel, owner, repo, branch):
        for s in self.store.guild_subs(interaction.guild_id):
            if s["channel"] == channel.id and same_thing(s, owner, repo, branch):
                return (f"Already tracking **{sub_label(s)}** in {channel.mention}. "
                        "To change who gets pinged, use `/pingrole`.")
        return None

    @staticmethod
    def ping_note(sub, role, channel):
        when = "when there's a crypto alert" if sub.get("ping_on") == "crypto" else "on every update"
        who = "@everyone" if role.is_default() else role.mention
        problem = ping_problem(role, channel)
        return f"Pings {who} {when}." + (f"\n**Heads up:** {problem}" if problem else "")

    async def start_repo(self, interaction, channel, owner, repo, branch):
        info = await asyncio.to_thread(gh.repo_info, owner, repo, self.github_token)
        full, owner, repo = info["full_name"], info["owner"]["login"], info["name"]
        if branch == info.get("default_branch"):
            branch = None  # the same as tracking the repo itself
        duplicate = self.already_tracked(interaction, channel, owner, repo, branch)
        if duplicate:
            return duplicate
        commits, _ = await asyncio.to_thread(gh.fetch_commits, full, branch, self.github_token)
        latest = commits[0] if commits else None
        t = self.store.targets.setdefault(target_key(full, branch), {"etag": None, "channels": {}})
        if not self.store.tracks(str(channel.id), full, branch):
            # Start from here, unless this channel already follows the repo through its account:
            # then it keeps its place, so commits not yet checked still get posted.
            t["channels"][str(channel.id)] = latest["sha"] if latest else None
        sub = {"id": secrets.token_hex(4), "guild": interaction.guild_id, "channel": channel.id,
               "owner": owner, "repo": repo, "branch": branch, "added_by": interaction.user.id}
        embed = gh.make_embed(
            f"Now tracking {sub_label(sub)}",
            f"New commits will be posted in {channel.mention}, with an alert when they mention a "
            f"$ticker or a wallet or contract address.\n\n"
            + ("Latest: " + gh.commit_line(latest) if latest else "No commits yet."),
            url=f"https://github.com/{full}" + (f"/tree/{branch}" if branch else ""))
        return sub, embed

    async def start_account(self, interaction, channel, owner):
        info = await asyncio.to_thread(gh.account_info, owner, self.github_token)
        login = info["login"]
        duplicate = self.already_tracked(interaction, channel, login, None, None)
        if duplicate:
            return duplicate
        listing, etag = await asyncio.to_thread(gh.account_repos, login, self.github_token)
        names = [r["name"] for r in listing if not r["archived"]]
        if login.lower() not in self.store.accounts:
            # Only for a new account: an existing entry is how the next check spots newly created repos.
            self.store.accounts[login.lower()] = Store.account_entry(listing, etag)
        sub = {"id": secrets.token_hex(4), "guild": interaction.guild_id, "channel": channel.id,
               "owner": login, "repo": None, "branch": None, "added_by": interaction.user.id}
        listed = "\n".join(f"• {n}" for n in names[:10]) + (f"\n• ...and {len(names) - 10} more" if len(names) > 10 else "")
        embed = gh.make_embed(
            f"Now tracking {sub_label(sub)}",
            f"{len(names)} public repo{'' if len(names) == 1 else 's'}. New commits will be posted in "
            f"{channel.mention}, and repos {login} creates later are picked up automatically."
            + (f"\n\n{listed}" if names else ""),
            url=f"https://github.com/{login}")
        return sub, embed

    def find_subs(self, interaction, what):
        """This server's subscriptions matching a pick from the list (an id) or a pasted GitHub link."""
        subs = self.store.guild_subs(interaction.guild_id)
        matches = [s for s in subs if s["id"] == what.strip()]
        if not matches:
            try:
                owner, repo, branch = gh.parse_link(what)
            except ValueError:
                return []
            same_repo = [s for s in subs if s["owner"].lower() == owner.lower()
                         and (s["repo"] or "").lower() == (repo or "").lower()]
            matches = [s for s in same_repo if branch is None or s["branch"] == branch]
            if not matches and branch:  # a .../tree/main link for a repo tracked as a whole
                matches = [s for s in same_repo if s["branch"] is None]
        return matches

    async def remove_tracking(self, interaction, what):
        matches = self.find_subs(interaction, what)
        if not matches:
            await interaction.response.send_message(
                "I'm not tracking that here. Use /list to see what's tracked.", ephemeral=True)
            return
        ids = {s["id"] for s in matches}
        # No waiting for a running check: Discord needs an answer within 3 seconds.
        self.store.drop_subs(lambda s: s["id"] in ids, f"{interaction.user} used /remove")
        lines = "\n".join(f"• {sub_label(s)} in <#{s['channel']}>" for s in matches)
        await interaction.response.send_message(f"Stopped tracking:\n{lines}")

    async def set_ping(self, interaction, what, role, ping_on):
        matches = self.find_subs(interaction, what)
        if matches and matches[0]["id"] != what.strip():
            # A pasted link: only change what's meant, not every match in the server.
            if any(s["branch"] is None for s in matches):
                matches = [s for s in matches if s["branch"] is None]  # the repo itself, not its branches
            if len({s["channel"] for s in matches}) > 1:
                here = [s for s in matches if s["channel"] == interaction.channel_id]
                if not here:
                    await interaction.response.send_message(
                        "That's tracked in more than one channel. Pick the one you mean from the list "
                        "that pops up for `what`, or run this in that channel.", ephemeral=True)
                    return
                matches = here
        if not matches:
            await interaction.response.send_message(
                "I'm not tracking that here. Use /list to see what's tracked.", ephemeral=True)
            return
        if role:
            for s in matches:
                channel = interaction.guild.get_channel(s["channel"])
                refused = channel and ping_not_allowed(interaction.user, role, channel)
                if refused:
                    await interaction.response.send_message(refused, ephemeral=True)
                    return
        notes = []
        for s in matches:  # no await in this loop, so a running check sees all or none of it
            s["ping"], s["ping_on"] = (role.id, ping_on) if role else (None, "all")
            channel = interaction.guild.get_channel(s["channel"])
            if role and channel:
                notes.append(f"**{sub_label(s)}** in {channel.mention}: {self.ping_note(s, role, channel)}")
            else:
                notes.append(f"**{sub_label(s)}** in <#{s['channel']}>: no pings any more.")
        self.store.save()
        log.info("%s set pings for %s", interaction.user, ", ".join(sub_label(s) for s in matches))
        await interaction.response.send_message("\n".join(notes)[:2000])

    async def remove_choices(self, interaction, current):
        choices = []
        for s in self.store.guild_subs(interaction.guild_id):
            channel = interaction.guild.get_channel(s["channel"]) if interaction.guild else None
            name = f"{sub_label(s)} → #{channel.name if channel else 'deleted-channel'}"
            if current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name[:100], value=s["id"]))
        return choices[:25]

    async def list_tracking(self, interaction):
        subs = self.store.guild_subs(interaction.guild_id)
        if not subs:
            await interaction.response.send_message(
                "Nothing is tracked here yet. Use `/add` with a GitHub link.", ephemeral=True)
            return
        by_channel = {}
        for s in subs:
            by_channel.setdefault(s["channel"], []).append(s)
        parts = []
        for channel_id, channel_subs in by_channel.items():
            lines = []
            for s in channel_subs:
                extra = ""
                if not s["repo"]:
                    n = len(self.store.accounts.get(s["owner"].lower(), {}).get("repos", []))
                    extra = f" - {n} repo{'' if n == 1 else 's'}"
                lines.append(f"• {sub_label(s)}{extra}{ping_label(s)}")
            parts.append(f"<#{channel_id}>\n" + "\n".join(lines))
        embed = discord.Embed(title="Tracked in this server", description="\n\n".join(parts)[:4096], color=gh.GREEN)
        embed.set_footer(text=f"Checked every {self.poll_minutes} minutes")
        await interaction.response.send_message(embed=embed, ephemeral=True)


def add_commands(bot):
    @app_commands.command(name="add", description="Track a GitHub repo, branch, or account")
    @app_commands.describe(link="A GitHub link: a repo, a branch (…/tree/name), or an account",
                           channel="Where to post (default: this channel)",
                           ping="A role to ping when this posts (optional)",
                           ping_on="When to ping that role (default: every update)")
    @app_commands.choices(ping_on=PING_ON)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def add(interaction: discord.Interaction, link: str, channel: Optional[discord.TextChannel] = None,
                  ping: Optional[discord.Role] = None, ping_on: Optional[app_commands.Choice[str]] = None):
        await bot.add_tracking(interaction, link, channel, ping, ping_on.value if ping_on else "all")

    @app_commands.command(name="pingrole", description="Choose a role to ping for something already tracked")
    @app_commands.describe(what="Pick one from the list, or paste its GitHub link",
                           role="The role to ping (leave empty to stop pinging)",
                           ping_on="When to ping it (default: every update)")
    @app_commands.choices(ping_on=PING_ON)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def pingrole(interaction: discord.Interaction, what: str, role: Optional[discord.Role] = None,
                       ping_on: Optional[app_commands.Choice[str]] = None):
        await bot.set_ping(interaction, what, role, ping_on.value if ping_on else "all")

    @pingrole.autocomplete("what")
    async def pingrole_autocomplete(interaction: discord.Interaction, current: str):
        return await bot.remove_choices(interaction, current)

    @app_commands.command(name="remove", description="Stop tracking a repo or account")
    @app_commands.describe(what="Pick one from the list, or paste its GitHub link")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def remove(interaction: discord.Interaction, what: str):
        await bot.remove_tracking(interaction, what)

    @remove.autocomplete("what")
    async def remove_autocomplete(interaction: discord.Interaction, current: str):
        return await bot.remove_choices(interaction, current)

    @app_commands.command(name="list", description="Show what this server tracks")
    @app_commands.guild_only()
    async def list_(interaction: discord.Interaction):
        await bot.list_tracking(interaction)

    for command in (add, remove, pingrole, list_):
        bot.tree.add_command(command)


def main():
    updater.setup_logging()
    updater.load_env()
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        log.error("DISCORD_TOKEN is missing from .env. Run install.ps1 again.")
        return EXIT_BAD_TOKEN
    try:
        poll_minutes = max(1, int(os.environ.get("POLL_MINUTES", "10")))
    except ValueError:
        poll_minutes = 10
    discord.VoiceClient.warn_nacl = False  # the bot has no voice features; skip the startup warnings
    discord.VoiceClient.warn_dave = False
    bot = TrackerBot(os.environ.get("GITHUB_TOKEN", "").strip(), poll_minutes)
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token in .env. Run install.ps1 again with a fresh token.")
        return EXIT_BAD_TOKEN
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
        log.error("Couldn't connect to Discord (%s). Check the VPS's internet connection; trying again in a minute", e)
        return 1
    except Exception as e:
        log.error("The bot stopped because of an error: %s: %s; trying again in a minute",
                  type(e).__name__, e, exc_info=True)
        return 1
    return EXIT_UPDATED if bot.restart_for_update else 0


if __name__ == "__main__":
    if not updater.single_instance():
        sys.exit("The bot is already running.")
    sys.exit(main())
