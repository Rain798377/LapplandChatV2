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
INTERNAL_MODULES = ["core.config", "core.ai", "core.memory", "commands.downloader_cmds",
                    "commands.random_cmds", "commands.memory_cmds", "commands.misc_cmds",
                    "commands.spotify_cmds", "commands.call_cmds", "services.voice_call.pipeline",
                    "services.voice_call.sink", "services.voice_call.stt", "services.voice_call.tts"]

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
    "core/config.py",
    "core/ai.py",
    "core/memory.py",
    "commands/downloader_cmds.py",
    "commands/random_cmds.py",
    "commands/memory_cmds.py",
    "commands/misc_cmds.py",
    "commands/spotify_cmds.py",
    "commands/call_cmds.py",
    "services/voice_call/pipeline.py",
    "services/voice_call/sink.py",
    "services/voice_call/stt.py",
    "services/voice_call/tts.py",
    "services/voice_call/audio.py",
    "services/voice_call/dave_patch.py",
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
THIRD_PARTY = ["discord", "discord.ext.voice_recv", "groq", "yt_dlp", "aiohttp", "PIL", "requests"]

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