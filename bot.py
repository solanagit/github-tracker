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

    def tracks(self, channel, full_name, branch):
        """Whether `channel` (an id string) still tracks this repo+branch, directly or via its account."""
        key = target_key(full_name, branch)
        for s in self.subs:
            if str(s["channel"]) != channel:
                continue
            if s["repo"] and target_key(f"{s['owner']}/{s['repo']}", s["branch"]) == key:
                return True
            if not s["repo"] and branch is None and any(
                    target_key(n, None) == key for n in self.account_repo_names(s["owner"])):
                return True
        return False

    def tracks_account(self, channel, owner):
        return any(not s["repo"] and s["owner"].lower() == owner.lower() and str(s["channel"]) == channel
                   for s in self.subs)

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
            for key in list(store.targets):
                if key not in wanted:
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
        for r in listing:
            old_name = known_ids.get(r["id"])
            if old_name is None and not r["archived"]:
                # Created, made public or moved into the account. Start from its very first commit,
                # so its first content is posted and scanned (that's where a launch shows up).
                t = store.targets.setdefault(target_key(r["name"], None), {"etag": None, "channels": {}})
                for channel in sorted(channels):
                    if self.store.tracks_account(channel, owner):  # not /removed in the meantime
                        t["channels"].setdefault(channel, None)
                        await self.deliver(int(channel), [gh.new_repo_embed(r["name"])])
            elif old_name is not None and old_name != r["name"]:
                # Renamed: carry over what was already posted and reported, so nothing is repeated.
                old_key, new_key = target_key(old_name, None), target_key(r["name"], None)
                if old_key in store.targets:
                    store.targets[new_key] = store.targets.pop(old_key)
                for seen_key in [k for k in store.seen if k.rsplit("#", 1)[0] == old_name.lower()]:
                    store.seen[f"{r['name'].lower()}#{seen_key.rsplit('#', 1)[1]}"] = store.seen.pop(seen_key)
                log.info("%s was renamed to %s", old_name, r["name"])

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
            if not self.store.tracks(channel, w["name"], w["branch"]):
                continue  # /removed while this check was running
            if await self.deliver(int(channel), [commits_post] + ([alert] if alert else [])):
                t["channels"][channel] = head
                if leftover:  # what didn't fit goes in one short follow-up
                    extra, keys = detect.leftover_embed(w["name"], leftover, change["url"])
                    if await self.deliver(int(channel), [extra]):
                        shown = shown + keys
                if shown:
                    self.store.seen[seen_key] = (seen + shown)[-1000:]
            else:
                delivered_all = False
        t["etag"] = etag if delivered_all else None  # a failed post is retried next time

    async def deliver(self, channel_id, embeds):
        """Posts embeds (dicts). False means try again later; True means done, or impossible."""
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            await channel.send(embeds=[discord.Embed.from_dict(e) for e in embeds])
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

    async def add_tracking(self, interaction, link, channel):
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
                return f"Already tracking **{sub_label(s)}** in {channel.mention}."
        return None

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
        t["channels"][str(channel.id)] = latest["sha"] if latest else None  # start from here
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

    async def remove_tracking(self, interaction, what):
        subs = self.store.guild_subs(interaction.guild_id)
        matches = [s for s in subs if s["id"] == what.strip()]
        if not matches:
            try:
                owner, repo, branch = gh.parse_link(what)
            except ValueError:
                owner = None
            if owner:
                same_repo = [s for s in subs if s["owner"].lower() == owner.lower()
                             and (s["repo"] or "").lower() == (repo or "").lower()]
                matches = [s for s in same_repo if branch is None or s["branch"] == branch]
                if not matches and branch:  # a .../tree/main link for a repo tracked as a whole
                    matches = [s for s in same_repo if s["branch"] is None]
        if not matches:
            await interaction.response.send_message(
                "I'm not tracking that here. Use /list to see what's tracked.", ephemeral=True)
            return
        ids = {s["id"] for s in matches}
        # No waiting for a running check: Discord needs an answer within 3 seconds.
        self.store.drop_subs(lambda s: s["id"] in ids, f"{interaction.user} used /remove")
        lines = "\n".join(f"• {sub_label(s)} in <#{s['channel']}>" for s in matches)
        await interaction.response.send_message(f"Stopped tracking:\n{lines}")

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
                lines.append(f"• {sub_label(s)}{extra}")
            parts.append(f"<#{channel_id}>\n" + "\n".join(lines))
        embed = discord.Embed(title="Tracked in this server", description="\n\n".join(parts)[:4096], color=gh.GREEN)
        embed.set_footer(text=f"Checked every {self.poll_minutes} minutes")
        await interaction.response.send_message(embed=embed, ephemeral=True)


def add_commands(bot):
    @app_commands.command(name="add", description="Track a GitHub repo, branch, or account")
    @app_commands.describe(link="A GitHub link: a repo, a branch (…/tree/name), or an account",
                           channel="Where to post (default: this channel)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def add(interaction: discord.Interaction, link: str, channel: Optional[discord.TextChannel] = None):
        await bot.add_tracking(interaction, link, channel)

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

    for command in (add, remove, list_):
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
