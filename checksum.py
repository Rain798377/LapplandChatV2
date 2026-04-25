import sys
import importlib
import importlib.util
import hashlib
import ast
from pathlib import Path

# ── Expected files (relative to project root) ────────────────────────────────
EXPECTED_FILES = [
    "app/LapplandV2.py",
    "app/config.py",
    "app/ai.py",
    "app/memory.py",
    "app/commands/download.py",
    "app/commands/random_cmds.py",
    "app/commands/memory_cmds.py",
    "app/commands/misc_cmds.py",
    "data/memory.json",
]

# ── Expected third-party packages ─────────────────────────────────────────────
THIRD_PARTY = [
    "discord",
    "groq",
    "yt_dlp",
    "aiohttp",
    "PIL",
    "requests",
]

# ── ANSI colors ───────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):     print(f"  {GREEN}ok{RESET}    {msg}")
def fail(msg):   print(f"  {RED}fail{RESET}  {msg}")
def warn(msg):   print(f"  {YELLOW}warn{RESET}  {msg}")
def header(msg): print(f"\n{BOLD}{CYAN}{msg}{RESET}")


# ── 1. File existence ─────────────────────────────────────────────────────────
def check_files() -> int:
    header("[ 1/4 ] File existence")
    errors = 0
    for rel in EXPECTED_FILES:
        path = Path(rel)
        if path.exists():
            ok(rel)
        elif rel == "data/memory.json":
            warn(f"{rel}  (missing — will be created on first run)")
        else:
            fail(f"{rel}  NOT FOUND")
            errors += 1
    return errors


# ── 2. Checksums ──────────────────────────────────────────────────────────────
def check_checksums() -> dict[str, str]:
    header("[ 2/4 ] File checksums (sha256, first 12 chars)")
    checksums = {}
    for rel in EXPECTED_FILES:
        path = Path(rel)
        if path.exists() and path.suffix == ".py":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            checksums[rel] = digest
            ok(f"{rel:<45} {digest}")
    return checksums


# ── 3. Third-party packages ───────────────────────────────────────────────────
def check_third_party() -> int:
    header("[ 3/4 ] Third-party packages")
    errors = 0
    for pkg in THIRD_PARTY:
        if importlib.util.find_spec(pkg) is not None:
            ok(pkg)
        else:
            fail(f"{pkg}  NOT INSTALLED")
            errors += 1
    return errors


# ── 4. Internal imports ───────────────────────────────────────────────────────
def extract_imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return [f"<SyntaxError: {e}>"]

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
    return imports

INTERNAL_MODULES = {"config", "ai", "memory", "commands"}

def check_internal_imports() -> int:
    header("[ 4/4 ] Internal import graph")
    errors = 0
    py_files = [
        Path(f) for f in EXPECTED_FILES
        if f.endswith(".py") and Path(f).exists()
    ]

    for path in py_files:
        imports = extract_imports(path)

        if any("<SyntaxError" in i for i in imports):
            fail(f"{path}  — {imports[0]}")
            errors += 1
            continue

        internal = [i for i in imports if i in INTERNAL_MODULES]
        third    = [i for i in imports if i in THIRD_PARTY]

        print(f"\n  {BOLD}{path}{RESET}")
        if internal:
            ok(f"internal : {', '.join(sorted(set(internal)))}")
        if third:
            ok(f"external : {', '.join(sorted(set(third)))}")

        for mod in set(internal):
            # files in commands/ import from the parent app/ folder, so check both
            search_dirs = [path.parent, path.parent.parent]
            found = False
            for search_dir in search_dirs:
                if (search_dir / f"{mod}.py").exists() or (search_dir / mod).is_dir():
                    ok(f"  '{mod}' found")
                    found = True
                    break
            if not found:
                fail(f"  '{mod}' not found (looked in {path.parent}/ and {path.parent.parent}/)")
                errors += 1

    return errors


# ── Summary ───────────────────────────────────────────────────────────────────
def checksum():
    print(f"\n{BOLD}{'─' * 52}")
    print("  LapplandChatV2 — project checksum")
    print(f"{'─' * 52}{RESET}")
    print(f"  root: {Path('.').resolve()}")

    e1 = check_files()
    _  = check_checksums()
    e3 = check_third_party()
    e4 = check_internal_imports()

    total = e1 + e3 + e4

    print(f"\n{BOLD}{'─' * 52}")
    if total == 0:
        print(f"  {GREEN}All checks passed.{RESET}")
    else:
        print(f"  {RED}{total} error(s) found — see above.{RESET}")
    print(f"{'─' * 52}{RESET}\n")

    sys.exit(1 if total > 0 else 0)