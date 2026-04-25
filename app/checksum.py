import sys
import hashlib
from pathlib import Path

# ── ANSI colors ───────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):     print(f"  {GREEN}ok{RESET}    {msg}", flush=True)
def fail(msg):   print(f"  {RED}fail{RESET}  {msg}", flush=True)
def warn(msg):   print(f"  {YELLOW}warn{RESET}  {msg}", flush=True)
def header(msg): print(f"\n{BOLD}{CYAN}{msg}{RESET}", flush=True)


# ── 1. Internal modules ───────────────────────────────────────────────────────
INTERNAL_MODULES = ["config", "ai", "memory", "commands.download",
                    "commands.random_cmds", "commands.memory_cmds", "commands.misc_cmds"]

def check_imports() -> int:
    header("[ 1/3 ] Internal modules")
    errors = 0
    for mod in INTERNAL_MODULES:
        try:
            __import__(mod)
            ok(mod)
        except ImportError as e:
            fail(f"{mod}  —  {e}")
            errors += 1
    return errors


# ── 2. Checksums ──────────────────────────────────────────────────────────────
EXPECTED_FILES = [
    "LapplandV2.py",
    "config.py",
    "ai.py",
    "memory.py",
    "commands/download.py",
    "commands/random_cmds.py",
    "commands/memory_cmds.py",
    "commands/misc_cmds.py",
]

def check_checksums() -> int:
    header("[ 2/3 ] File checksums (sha256, first 12 chars)")
    errors = 0
    for rel in EXPECTED_FILES:
        path = Path(rel)
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            ok(f"{rel:<45} {digest}")
        else:
            fail(f"{rel}  NOT FOUND")
            errors += 1
    return errors


# ── 3. Third-party packages ───────────────────────────────────────────────────
THIRD_PARTY = ["discord", "groq", "yt_dlp", "aiohttp", "PIL", "requests"]

def check_third_party() -> int:
    header("[ 3/3 ] Third-party packages")
    errors = 0
    for pkg in THIRD_PARTY:
        try:
            __import__(pkg)
            ok(pkg)
        except ImportError:
            fail(f"{pkg}  NOT INSTALLED")
            errors += 1
    return errors


# ── Entry point ───────────────────────────────────────────────────────────────
def checksum():
    print(f"\n{BOLD}{'─' * 52}")
    print("  LapplandChatV2 — startup checksum")
    print(f"{'─' * 52}{RESET}", flush=True)

    e1 = check_imports()
    e2 = check_checksums()
    e3 = check_third_party()

    total = e1 + e2 + e3
    print(f"\n{BOLD}{'─' * 52}")
    if total == 0:
        print(f"  {GREEN}All checks passed.{RESET}")
    else:
        print(f"  {RED}{total} error(s) found — see above.{RESET}")
    print(f"{'─' * 52}{RESET}\n", flush=True)

    if total > 0:
        sys.exit(1)