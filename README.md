# GitHub Tracker

Posts new commits from any public GitHub repos to a Discord channel. It doesn't need
access to those repos and doesn't need webhooks set up on them.

It runs on a Windows server every 10 minutes and updates itself from this GitHub repo
before each check.

## What to track

Edit [`repos.txt`](repos.txt) right here on GitHub (pencil icon, then **Commit changes**).
Put one link per line:

```
https://github.com/owner/repo             a repo
https://github.com/owner/repo/tree/dev    a specific branch
https://github.com/owner                  every public repo of an account
```

The server picks up the change within 10 minutes and posts a "Now tracking" message.

## Setting up the server (once)

1. Install **Python** from https://www.python.org/downloads/. On the first installer
   screen, tick **Add python.exe to PATH**.
2. Install **Git** from https://git-scm.com/download/win. The default options are fine.
3. In Discord, create a webhook for your channel: **Edit Channel → Integrations →
   Webhooks → New Webhook → Copy Webhook URL**.
4. Open **PowerShell as Administrator** and run:

   ```powershell
   git clone https://github.com/OWNER/github-tracker C:\github-tracker
   cd C:\github-tracker
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   Paste the webhook URL when it asks.

The webhook URL is saved only on the server, in `C:\github-tracker\.env`. It never goes
to GitHub, because anyone with that URL can post in your channel.

## Day to day

- **See what it's doing:** open `C:\github-tracker\tracker.log`.
- **Stop it:** in an Administrator PowerShell, run
  `Unregister-ScheduledTask -TaskName 'GitHub Tracker' -Confirm:$false`.
- **Track more than about 9 repos:** without a token, GitHub limits how often it can
  be asked. Create a token at https://github.com/settings/personal-access-tokens/new
  (no permissions needed) and put it after `GITHUB_TOKEN=` in `.env` on the server.

Anyone who can push to this repo can change the code the server runs, so keep 2FA on
for your GitHub account.
