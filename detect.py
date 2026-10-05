"""Spots crypto in new commits: $TICKERS and wallet or contract addresses.

Looks at commit messages and at the lines the commits add. Addresses are checked on-chain where
possible to tell contracts from wallets: 0x addresses on six EVM chains, Solana addresses on
Solana. Everything here is plain blocking code; the bot runs it in a worker thread.

    find_crypto(...)    -> everything spotted in some commits
    build_alert(...)    -> the Discord alert for what a channel hasn't been told about yet
    leftover_embed(...) -> a short follow-up listing whatever didn't fit in the alert
"""
import concurrent.futures
import hashlib
import json
import logging
import re
import threading
import time
import urllib.request

import github_watch as gh

log = logging.getLogger("tracker")

ORANGE = 0xF0883E
ALERT_TITLE = "\N{POLICE CARS REVOLVING LIGHT} Crypto spotted in {}"
BATCH = 8  # findings checked on-chain at a time, most important first, until the alert is full

# ---------- Patterns ----------

TICKER = re.compile(r"(?<![\w$])\$([A-Z][A-Z0-9]{1,9})(?!\w)")
# In commit messages and prose, tickers are often written in lower or mixed case ($pnut, $ai16z).
# Not before "(", "[", "=" or ".word", which mean code ($route.params, $set = ...).
TICKER_ANY_CASE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})(?![\w(\[=]|\.\w|\s*=)")
NOT_TICKERS = {  # $NAMES that are almost always variables in scripts, CI files and docs
    "HOME", "PATH", "USER", "PWD", "OLDPWD", "SHELL", "TERM", "LANG", "EDITOR", "HOSTNAME", "UID",
    "IFS", "RANDOM", "SECONDS", "TEMP", "TMP", "TMPDIR", "PS1", "CI", "PORT", "HOST", "ENV", "NODE",
    "ARGV", "ARGS", "INPUT", "OUTPUT", "VERSION", "REPO", "BRANCH", "TAG", "DIR", "FILE", "FILES",
    "URL", "NAME", "PID", "PROFILE", "ARCH", "OUT", "DEST", "SRC", "CC", "CXX", "CFLAGS", "LDFLAGS",
    "DEBUG", "IMAGE", "RPC", "KEYPAIR", "MINT", "WALLET", "TOKEN", "NETWORK", "OWNER", "SECRET", "KEY",
    "SHA", "APP", "TARGET", "TARGETARCH", "GLOBALS", "DSN", "PASS", "PASSWORD", "DB", "AUTOLOAD",
    "ROOT", "BIN", "LIB", "CMD", "OS", "MODE", "LOG", "CONFIG", "DATA", "ID", "API", "STAGE",
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR",
}
NOT_TICKERS_ANY_CASE = {  # common lowercase $words: MongoDB, JSON Schema, Vue, jQuery, shell
    "set", "unset", "push", "pull", "inc", "match", "lookup", "group", "project", "sort", "limit",
    "in", "nin", "or", "and", "not", "nor", "exists", "regex", "ref", "schema", "id", "defs",
    "store", "route", "router", "this", "emit", "refs", "el", "props", "attrs", "slots", "data",
    "parent", "root", "event", "http", "scope", "window", "document", "args", "argv", "home", "path",
    "user", "env", "var", "val", "value", "key", "item", "items", "name", "file", "dir", "out",
}
SCRIPT_FILE = re.compile(r"(?:\.(?:sh|bash|zsh|fish|ps1|psm1|psd1|bat|cmd|php|pl|pm|mk|tf)$"
                         r"|(?:^|/)(?:Makefile|Dockerfile[^/]*|\.github/workflows/[^/]+)$)", re.I)
PROSE_FILE = re.compile(r"\.(?:md|markdown|txt|html?)$", re.I)
HTML_FILE = re.compile(r"\.html?$", re.I)
JS_LINE = re.compile(r"\b(?:const|let|var|function|return)\b|=>|[;{}]\s*$")
ASSIGNED_NAME = re.compile(r"(?m)^\s*(?:export\s+|ARG\s+|ENV\s+|set\s+|declare\s+(?:-\w+\s+)?)?"
                           r"([A-Za-z_][A-Za-z0-9_]*)\s*=")

EVM = re.compile(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]{40}(?![0-9A-Za-z])")
B58 = "1-9A-HJ-NP-Za-km-z"
# A run of base58 characters that isn't part of a longer token (base64, hex, identifiers), but may
# follow '=' (CA=..., ?outputMint=...) or sit in a swap link (jup.ag/swap/SOL-<mint>, <mint>-SOL).
BASE58 = re.compile(rf"(?:(?<![{B58}0OIl+=_-])|(?<==)|(?<=/SOL-)|(?<=/USDC-)|(?<=/USDT-))"
                    rf"[{B58}]{{32,44}}"
                    rf"(?:(?=-(?:SOL|USDC|USDT|WSOL)\b)|(?![{B58}0OIl+=_-]))")
BITCOIN_LEGACY = re.compile(rf"(?<![{B58}0OIl+=_-])[13][{B58}]{{25,34}}(?![{B58}0OIl+=_-])")
BITCOIN_BECH32 = re.compile(r"(?<![0-9A-Za-z])bc1[02-9ac-hj-np-z]{11,71}(?![0-9A-Za-z])")
SKIP_FILE = re.compile(r"(?:^|/)(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml"
                       r"|[^/]*\.lock|[^/]*\.min\.(?:js|css)|[^/]*\.map)$", re.I)
INTEGRITY = re.compile(r"integrity=|sha(?:256|384|512)-")
LONG_LINE = 1000            # longer lines (minified code, one-line pages) are scanned in windows,
WINDOW, WINDOW_STEP = 300, 240  # for addresses only; the overlap is longer than any address

CONTRACT_HINTS = ("contract", "token address", "mint", "dexscreener", "pump.fun", "birdeye", "dextools",
                  "gmgn", "uniswap", "raydium", "jup.ag", "pancakeswap", "geckoterminal")
CA_HINT = re.compile(r"(?<![\w$])ca\s*[:=]")  # "CA: ..." but not "const $ca = ..."
WALLET_HINTS = ("wallet", "treasury", "deployer", "multisig", "donat", "send to", "receiv", "payout")

# Addresses every crypto project mentions (programs, wrapped coins, stablecoins, routers): not news.
KNOWN_SOLANA = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo", "ComputeBudget111111111111111111111111111111",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s", "BPFLoaderUpgradeab1e11111111111111111111111",
    "BPFLoader2111111111111111111111111111111111", "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM", "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
}
KNOWN_EVM = {a.lower() for a in (
    "0x0000000000000000000000000000000000000000", "0xffffffffffffffffffffffffffffffffffffffff",
    "0x000000000000000000000000000000000000dEaD",
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "0xcA11bde05977b3631167028862bE2a173976CA11", "0x000000000022D473030F116dDEE9F6B43aC78BA3",
    "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789", "0x0000000071727De22E5E9d8BAf0edAc6f37da032",
    "0x4e59b44847b379578588920cA78FbF26c0B4956C", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    "0xE592427A0AEce92De3Edee1F18E0157C05861564", "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
    "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "0x10ED43C718714eb63d5aA57B78B54704E256024E", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "0xdAC17F958D2ee523a2206206994597C13D831ec7", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
)}
OP_STACK_PREDEPLOY = "0x42" + "0" * 36  # 0x4200...00XX system contracts on Base, Optimism, etc.

# ---------- Recognising addresses ----------

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58_ALPHABET.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def b58check(s, version):
    """True if `s` is a valid Base58Check string with this version byte (25 bytes long)."""
    raw = b58decode(s)
    return (len(raw) == 25 and raw[0] == version
            and hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:])


def is_solana(s):
    """A Solana address is 32 bytes. Real ones mix upper case, lower case and digits."""
    if s in KNOWN_SOLANA or s.startswith("Sysvar"):
        return False
    mixed = any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s)
    return mixed and len(b58decode(s)) == 32


def is_tron(s):
    return s.startswith("T") and len(s) == 34 and b58check(s, 0x41)


def is_bitcoin_legacy(s):
    return (s[0] == "1" and b58check(s, 0x00)) or (s[0] == "3" and b58check(s, 0x05))


def is_known_evm(address):
    low = address.lower()
    return low in KNOWN_EVM or low[:40] == OP_STACK_PREDEPLOY


def line_hint(low):
    if any(h in low for h in CONTRACT_HINTS) or CA_HINT.search(low):
        return "contract"
    if any(h in low for h in WALLET_HINTS):
        return "wallet"
    return None


def patch_lines(patch):
    """A diff as scan() input: (True, text) added, (False, text) unchanged, None at each new hunk."""
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            yield None
        elif raw.startswith("+") and not raw.startswith("+++"):
            yield True, raw[1:]
        elif raw.startswith(" "):
            yield False, raw[1:]


def scan_text(text, where, found, filename=None, prose=False):
    """scan() for plain text, all of it new (e.g. a commit message)."""
    scan([(True, line) for line in text.splitlines()], where, found, filename, prose)


def starts_in_code_block(lines, start):
    """Whether the hunk starting at lines[start] begins inside a ``` code block.

    A diff hunk can start in the middle of a block. Then its first fence is a closing one,
    which shows as an odd number of fences that starts with a bare ``` (an opening fence
    can name a language, a closing one can't).
    """
    fences = []
    for item in lines[start + 1 if lines[start] is None else start:]:
        if item is None:
            break
        stripped = item[1].strip()
        if stripped.startswith("```"):
            fences.append(bool(stripped[3:].strip()))
    return len(fences) % 2 == 1 and not fences[0]


def scan(lines, where, found, filename=None, prose=False):
    """Adds what the added lines mention to `found` ({key: finding}); `where` says where it was seen.

    `lines` holds (added, text) pairs and None at the start of each diff hunk. Unchanged lines
    are only read for context: whether we're in a code block, or a "Contract:" label just above.
    `filename` decides how strict ticker matching is (none in scripts, any case in prose);
    `prose=True` is for commit messages.
    """
    lines = list(lines)
    is_prose = prose or bool(filename and PROSE_FILE.search(filename))
    is_html = bool(filename and HTML_FILE.search(filename))
    tickers_allowed = not (filename and SCRIPT_FILE.search(filename))
    assigned = set(ASSIGNED_NAME.findall("\n".join(item[1] for item in lines if item)))  # NAME=... -> $NAME is a variable
    in_fence = in_script = False
    recent_hints = []
    for i, item in enumerate(lines):
        if item is None or i == 0:  # a new hunk: what came before is a different part of the file
            in_fence, in_script, recent_hints = starts_in_code_block(lines, i), False, []
            if item is None:
                continue
        added, line = item
        stripped, low_line = line.strip(), line.lower()
        if stripped.startswith("```"):
            # A fence with a language name (```bash) always opens a block; a bare one toggles.
            in_fence = True if stripped[3:].strip() else not in_fence
            continue
        script_line = in_script or "<script" in low_line
        if is_html:
            opens, closes = low_line.count("<script"), low_line.count("</script>")
            if opens != closes:
                in_script = opens > closes
        if not added:
            if stripped:  # an unchanged label line can describe an added address below it
                has_address = EVM.search(line) or BASE58.search(line) or BITCOIN_BECH32.search(line)
                recent_hints = (recent_hints + [None if has_address else line_hint(low_line)])[-2:]
            continue

        long_line = len(line) > LONG_LINE
        starts = range(0, len(line), WINDOW_STEP) if long_line else [0]
        hint_here, line_hits = None, 0
        for start in starts:
            window = line[start:start + WINDOW] if long_line else line
            if INTEGRITY.search(window):
                continue  # integrity hashes are full of look-alikes
            hint = line_hint(window.lower())
            hint_here = hint_here or hint
            # A "## Contract" heading or "CA:" label often sits just above the address.
            hint = hint or next((h for h in reversed(recent_hints) if h), None)

            def whole(m):
                """False for a match cut off by the edge of a window (a piece of a longer value)."""
                if not long_line:
                    return True
                return not ((m.start() == 0 and start > 0) or (m.end() == len(window) and start + WINDOW < len(line)))

            hits = []
            shell_line = low_line.lstrip().startswith(("$ ", "export "))
            if tickers_allowed and not long_line and not (in_fence and filename) and not shell_line:
                code_like = is_html and (script_line or JS_LINE.search(line))
                pattern = TICKER_ANY_CASE if is_prose and not code_like else TICKER
                for m in pattern.finditer(window):
                    name = m.group(1)
                    if name.upper() in NOT_TICKERS or name.lower() in NOT_TICKERS_ANY_CASE or name in assigned:
                        continue
                    hits.append(("ticker", "$" + name))
            hits += [("evm", m.group(0)) for m in EVM.finditer(window)
                     if whole(m) and not is_known_evm(m.group(0))]
            for m in BASE58.finditer(window):
                if not whole(m):
                    continue
                if is_tron(m.group(0)):
                    hits.append(("tron", m.group(0)))
                elif is_solana(m.group(0)):
                    hits.append(("solana", m.group(0)))
            hits += [("bitcoin", m.group(0)) for m in BITCOIN_LEGACY.finditer(window)
                     if whole(m) and is_bitcoin_legacy(m.group(0))]
            hits += [("bitcoin", m.group(0)) for m in BITCOIN_BECH32.finditer(window) if whole(m)]
            line_hits += sum(kind != "ticker" for kind, _ in hits)
            for kind, value in hits:
                key = f"{kind}:{value.upper() if kind == 'ticker' else value.lower() if kind == 'evm' else value}"
                if key not in found:
                    found[key] = {"key": key, "kind": kind, "value": value, "hint": hint, "where": where}
                elif hint and not found[key]["hint"]:
                    found[key]["hint"] = hint
        if stripped:
            # Only a label without its own address ("## Contract", "Dev wallet:") describes the lines below.
            recent_hints = (recent_hints + [None if line_hits else hint_here])[-2:]


def find_crypto(full_name, new, files, token):
    """Everything spotted in some new commits (oldest first): {key: finding}.

    `files` is the diff of exactly those commits, or None to fetch it here. Raises
    github_watch.RateLimited if GitHub refuses, so the caller can retry later.
    """
    found = {}
    for c in new:
        scan_text(c["commit"]["message"], f"commit `{c['sha'][:7]}`", found, prose=True)
    if files is None:
        files = []
        head = new[-1]["sha"]
        parents = new[0].get("parents") or []
        try:
            if parents:
                files = gh.github_get(f"/repos/{full_name}/compare/{parents[0]['sha']}...{head}", token)[0].get("files") or []
            else:  # a repo's very first commit has nothing to compare with, so read it directly
                files = gh.commit_detail(full_name, new[0]["sha"], token).get("files") or []
                if len(new) > 1:
                    files += gh.github_get(f"/repos/{full_name}/compare/{new[0]['sha']}...{head}", token)[0].get("files") or []
        except gh.RateLimited:
            raise
        except Exception as e:
            log.warning("%s: couldn't read the changed lines (%s); checked commit messages only", full_name, e)
    for f in files:
        if SKIP_FILE.search(f["filename"]) or not f.get("patch"):
            continue
        scan(patch_lines(f["patch"]), f"`{f['filename']}`", found, filename=f["filename"])
    return found


# ---------- On-chain checks (in parallel, cached) ----------

EVM_CHAINS = [  # (name, public RPC, explorer)
    ("Ethereum", "https://ethereum-rpc.publicnode.com", "https://etherscan.io/address/"),
    ("Base", "https://base-rpc.publicnode.com", "https://basescan.org/address/"),
    ("BNB Chain", "https://bsc-rpc.publicnode.com", "https://bscscan.com/address/"),
    ("Arbitrum", "https://arbitrum-one-rpc.publicnode.com", "https://arbiscan.io/address/"),
    ("Polygon", "https://polygon-bor-rpc.publicnode.com", "https://polygonscan.com/address/"),
    ("Optimism", "https://optimism-rpc.publicnode.com", "https://optimistic.etherscan.io/address/"),
]
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SOLANA_TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}
SOLANA_SYSTEM_PROGRAM = "11111111111111111111111111111111"
CACHE_SECONDS, FAILURE_CACHE_SECONDS = 6 * 3600, 120
CHECK_DEADLINE = 20  # seconds for all on-chain checks of one batch together
_cache, _cache_lock = {}, threading.Lock()
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
FAILED = object()


def rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": gh.USER_AGENT})
    with urllib.request.urlopen(req, timeout=6) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def from_cache(key):
    """The cached answer, FAILED for a recent failure, or None if we need to ask."""
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < (FAILURE_CACHE_SECONDS if hit[1] is FAILED else CACHE_SECONDS):
        return hit[1]
    return None


def to_cache(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), FAILED if value is None else value)


def evm_has_code(chain, address):
    """True/False, or None if the chain couldn't be asked."""
    name, url, _ = chain
    key = ("evm", name, address.lower())
    hit = from_cache(key)
    if hit is not None:
        return None if hit is FAILED else hit
    try:
        code = rpc(url, "eth_getCode", [address, "latest"])
        # 0xef0100... is a wallet that delegates to a contract (EIP-7702), not a contract itself.
        answer = bool(code) and code not in ("0x", "0x0") and not code.startswith("0xef0100")
    except Exception as e:
        log.info("Couldn't check %s on %s: %s", address, name, e)
        answer = None
    to_cache(key, answer)
    return answer


def solana_account_kind(value):
    if value is None:
        return "unused"
    if value.get("executable"):
        return "program"
    if value.get("owner") in SOLANA_TOKEN_PROGRAMS:
        data = value.get("data")
        return "token" if isinstance(data, dict) and data.get("parsed", {}).get("type") == "mint" else "account"
    if value.get("owner") == SOLANA_SYSTEM_PROGRAM:
        return "wallet"
    return "account"


def solana_account_types(addresses):
    """{address: 'token' | 'program' | 'wallet' | 'unused' | 'account' | None (check failed)}, in one request."""
    answers, todo = {}, []
    for a in addresses:
        hit = from_cache(("solana", a))
        if hit is None:
            todo.append(a)
        else:
            answers[a] = None if hit is FAILED else hit
    if todo:
        try:
            values = rpc(SOLANA_RPC, "getMultipleAccounts", [todo, {"encoding": "jsonParsed"}])["value"]
            fresh = {a: solana_account_kind(v) for a, v in zip(todo, values)}
        except Exception as e:
            log.info("Couldn't check %d address(es) on Solana: %s", len(todo), e)
            fresh = {a: None for a in todo}
        for a, kind in fresh.items():
            to_cache(("solana", a), kind)
        answers.update(fresh)
    return answers


def check_on_chain(findings):
    """Runs the on-chain checks these findings need, in parallel, within CHECK_DEADLINE.

    Returns {key: result}: for 0x addresses a list (one True/False/None per chain), for Solana
    addresses a kind or None. Unfinished checks count as failed.
    """
    evm = {f["key"]: [_pool.submit(evm_has_code, chain, f["value"]) for chain in EVM_CHAINS]
           for f in findings if f["kind"] == "evm"}
    solana = [f for f in findings if f["kind"] == "solana"]
    solana_job = _pool.submit(solana_account_types, [f["value"] for f in solana]) if solana else None
    jobs = [j for js in evm.values() for j in js] + ([solana_job] if solana_job else [])
    done, pending = concurrent.futures.wait(jobs, timeout=CHECK_DEADLINE)
    for job in pending:
        job.cancel()

    def result(job):
        return job.result() if job in done and job.exception() is None else None
    results = {key: [result(j) for j in js] for key, js in evm.items()}
    kinds = result(solana_job) if solana_job else None
    for f in solana:
        results[f["key"]] = (kinds or {}).get(f["value"])
    return results


# ---------- The alert ----------

def label_and_links(f, on_chain):
    """(label, links) for a finding, or None to leave it out (a random string, not an address)."""
    kind, v, hint = f["kind"], f["value"], f["hint"]
    if kind == "ticker":
        return "Ticker", [f"[DexScreener](https://dexscreener.com/search?q={v[1:]})"]
    if kind == "evm":
        answers = on_chain or [None] * len(EVM_CHAINS)
        with_code = [chain for chain, has in zip(EVM_CHAINS, answers) if has]
        asked = [chain[0] for chain, has in zip(EVM_CHAINS, answers) if has is not None]
        dex = f"[DexScreener](https://dexscreener.com/search?q={v})"
        if with_code:
            return ("Contract on " + ", ".join(c[0] for c in with_code),
                    [f"[{c[0]}]({c[2]}{v})" for c in with_code[:3]] + [dex])
        links = [f"[Etherscan](https://etherscan.io/address/{v})", dex]
        if hint == "wallet":
            return "Wallet (EVM)", links
        where = f"no contract on {', '.join(asked)}" if asked else "couldn't check the chains"
        if hint == "contract":
            return f"Contract address, not deployed yet? ({where})", links
        return f"EVM address ({where})", links
    if kind == "solana":
        looks_like_token = hint == "contract" or v.endswith("pump")
        if on_chain == "unused" and not looks_like_token and hint != "wallet":
            return None  # never used on Solana and nothing says it's an address: most likely a random ID
        label = {
            "token": "Token contract (Solana)",
            "program": "Program (Solana)",
            "wallet": "Wallet (Solana)",
            "account": "Solana account",
            "unused": "Token contract (Solana, not live yet)" if looks_like_token else "Wallet (Solana, not used yet)",
        }.get(on_chain) or ("Contract address (Solana)" if looks_like_token else "Wallet (Solana)")
        if on_chain == "token" or looks_like_token:
            links = [f"[DexScreener](https://dexscreener.com/solana/{v})", f"[Solscan](https://solscan.io/token/{v})"]
        else:
            links = [f"[Solscan](https://solscan.io/account/{v})"]
        if v.endswith("pump"):
            links.append(f"[pump.fun](https://pump.fun/coin/{v})")
        return label, links
    if kind == "tron":
        return ({"contract": "Contract address (TRON)", "wallet": "Wallet (TRON)"}.get(hint, "TRON address"),
                [f"[Tronscan](https://tronscan.org/#/address/{v})"])
    return "Bitcoin wallet", [f"[mempool.space](https://mempool.space/address/{v})"]


def importance(f):
    """Sort order for an alert: likely token contracts first, then where it was said, then tickers."""
    w = f["where"].lower()
    if f["hint"] == "contract" or f["value"].endswith("pump"):
        return 0
    if w.startswith("commit") or any(x in w for x in ("readme", ".md", ".html", ".htm")):
        return 1
    return 2 if f["kind"] == "ticker" else 3


def build_alert(full_name, findings, seen, url, budget):
    """The alert for findings not in `seen`, within `budget` characters (title + text).

    Returns (embed or None, keys it reports, leftovers). Leftovers are findings that didn't fit
    (or couldn't be checked) and should go in a leftover_embed() follow-up. Findings that turn
    out not to be real addresses are dropped.
    """
    fresh = sorted((f for k, f in findings.items() if k not in seen), key=importance)
    title = ALERT_TITLE.format(full_name)[:256]
    room = min(4096, budget - len(title)) - 30
    parts, shown, leftover = [], [], []
    for i in range(0, len(fresh), BATCH):
        batch = fresh[i:i + BATCH]
        if leftover:  # the alert is full: the rest only get listed, without on-chain checks
            leftover += batch
            continue
        on_chain = check_on_chain(batch)
        for f in batch:
            result = on_chain.get(f["key"])
            unverified = (f["kind"] == "solana" and result is None and f["hint"] is None
                          and not f["value"].endswith("pump"))
            described = None if unverified else label_and_links(f, result)
            if unverified:
                leftover.append(f)  # couldn't check it, and nothing says it's an address: list it only
                continue
            if described is None:
                continue
            label, links = described
            part = f"**{label}**\n`{f['value']}`\nin {f['where']} · " + " · ".join(links)
            if leftover or len("\n\n".join(parts + [part])) > room:
                leftover.append(f)
            else:
                parts.append(part)
                shown.append(f["key"])
    if not parts:
        return None, [], leftover
    return gh.make_embed(title, "\n\n".join(parts), url=url, color=ORANGE), shown, leftover


def leftover_embed(full_name, leftovers, url):
    """A compact follow-up listing findings that didn't fit in the alert. Returns (embed, keys)."""
    groups = {}
    for f in leftovers:
        name = {"ticker": "Tickers", "evm": "0x addresses", "solana": "Possible Solana addresses",
                "tron": "TRON addresses", "bitcoin": "Bitcoin addresses"}[f["kind"]]
        groups.setdefault(name, []).append(f)
    text, listed = "", 0
    for name, items in groups.items():
        section = f"**{name}**\n"
        for f in items:
            piece = f"`{f['value']}`\n"
            if len(text) + len(section) + len(piece) > 3900:
                break
            section += piece
            listed += 1
        text += section + "\n"
    if listed < len(leftovers):
        text += f"...and {len(leftovers) - listed} more. See the changes on GitHub."
    return (gh.make_embed(f"More crypto spotted in {full_name}"[:256], text.strip(), url=url, color=ORANGE),
            [f["key"] for f in leftovers])
