# GitHub Tracker

A Discord bot that posts new commits from any public GitHub repo, controlled with slash
commands in your server. When a commit mentions a **$TICKER** or adds a **wallet or contract
address**, it posts a highlighted alert as well.

It doesn't need access to the repos it tracks, or any setup on them.

## Commands

| Command | What it does |
|---|---|
| `/add link [channel]` | Track a repo (`https://github.com/owner/repo`), one branch (`…/tree/dev`), or every public repo of an account (`https://github.com/owner`). Posts go to the channel you run it in, or the one you pick. |
| `/remove what` | Stop tracking something. Pick it from the list that pops up. |
| `/list` | Show what this server tracks. |

`/add` and `/remove` are for people with the **Manage Server** permission. To let a role
use them, go to **Server Settings → Integrations → GitHub Tracker**.

When a tracked account creates a new repo, the bot announces it and posts and scans its
first commits too.

## Crypto alerts

The bot checks commit messages and every line the new commits add for:

- **Tickers** such as `$IMDT` or `$pnut`
- **0x addresses**. It checks Ethereum, Base, BNB Chain, Arbitrum, Polygon and Optimism to
  tell a contract from a wallet.
- **Solana addresses**. It checks Solana to tell a token contract from a wallet, and
  spots pump.fun addresses.
- **TRON** and **Bitcoin** addresses

Each one is reported once per repo in each channel, the first time it shows up. The most
likely token contracts come first, with links to DexScreener and the right block explorer.
If a commit adds more than fit in one alert, the rest are listed in a short follow-up.
Addresses every project mentions, like USDC or the Solana token program, are left out.

## Setting up (once)

### 1. Create the Discord bot

1. Go to https://discord.com/developers/applications and click **New Application**.
   Name it (for example "GitHub Tracker") and click **Create**.
2. Click **Bot** on the left, then **Reset Token**, and copy the token. Keep it private:
   anyone with it can control your bot. You'll paste it into the installer and nowhere else.

### 2. Set up the Windows VPS

1. Install **Python**: go to https://www.python.org/downloads/windows/ and download the
   **Windows installer (64-bit)** of the latest Python 3. On its first screen, tick
   **Add python.exe to PATH**, then click **Install Now**.
2. Install **Git** from https://git-scm.com/download/win. The default options are fine.
3. Open **PowerShell as Administrator** and run:

   ```powershell
   git clone https://github.com/OWNER/github-tracker C:\github-tracker
   cd C:\github-tracker
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

4. Paste the bot token when it asks. It stays hidden while you paste. It then asks for an
   optional GitHub token; press Enter to skip it.
5. It prints an **invite link**. Open it, pick your server, and click **Authorize**.

That's it. Type `/add` in your server.

The bot runs in the background, starts again by itself if the VPS restarts, and picks up
new versions from this GitHub repo within 10 minutes of each push.

## Good to know

- **Logs:** `C:\github-tracker\logs\bot.log`
- **Stop the bot:** in an Administrator PowerShell, run
  `Stop-ScheduledTask -TaskName 'GitHub Tracker'; Unregister-ScheduledTask -TaskName 'GitHub Tracker' -Confirm:$false`
- **Tracking more than about 7 repos:** without a token, GitHub limits how often it can be
  asked. Create a token at https://github.com/settings/personal-access-tokens/new. It needs
  no permissions; pick the longest expiry offered. Then run `install.ps1` again and paste it
  when it asks. When GitHub emails you that it's expiring, do the same with a new one. (If
  it does expire, the bot keeps working without it, just with the lower limit.)
- **Secrets:** the tokens stay on the VPS in `.env`, which is never uploaded to GitHub.
- Anyone who can push to this repo can change the code the VPS runs, so keep two-factor
  authentication on for your GitHub account.
