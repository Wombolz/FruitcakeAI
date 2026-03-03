#!/usr/bin/env python3
"""
Sprint 1 verification — run after: pip install -r requirements.txt, and optionally
  docker compose up -d postgres && uvicorn app.main:app --host 127.0.0.1 --port 8000

Usage:
  python3 scripts/verify_sprint1.py              # structure + (if server running) live checks
  python3 scripts/verify_sprint1.py --live-only   # only live HTTP checks (server must be running)
  python3 scripts/verify_sprint1.py --structure-only  # only file/structure checks
"""

import os
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

BASE_URL = os.environ.get("FRUITCAKE_BASE_URL", "http://127.0.0.1:8000")


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# ── Sprint 1.1: Bootstrap ────────────────────────────────────────────────────

def check_1_1_structure() -> int:
    section("Sprint 1.1 — Project bootstrap (structure)")
    errs = 0
    required = [
        ("app/main.py", "FastAPI app"),
        ("app/config.py", "Pydantic settings"),
        ("requirements.txt", "Dependencies"),
        (".env.example", "Env template"),
        ("docker-compose.yml", "Postgres + pgvector"),
        ("app/db/models.py", "SQLAlchemy models"),
        ("app/db/session.py", "Async session"),
        ("app/auth/router.py", "Auth router"),
        ("app/auth/jwt.py", "JWT helpers"),
        ("app/auth/dependencies.py", "Auth dependencies"),
        ("config/rag_config.yaml", "RAG config"),
    ]
    for path, desc in required:
        if (ROOT / path).exists():
            ok(f"{path} — {desc}")
        else:
            fail(f"{path} missing — {desc}")
            errs += 1
    return errs


def check_1_1_health_live() -> int:
    section("Sprint 1.1 — /health returns 200")
    errs = 0
    try:
        import httpx
        r = httpx.get(f"{BASE_URL}/health", timeout=5)
        if r.status_code == 200 and r.json().get("status") == "ok":
            ok("/health 200, status=ok")
        else:
            fail(f"/health returned {r.status_code} or wrong body")
            errs += 1
    except Exception as e:
        fail(f"Cannot reach {BASE_URL}/health — is server running? ({e})")
        errs += 1
    return errs


# ── Sprint 1.2: Auth ─────────────────────────────────────────────────────────

def check_1_2_structure() -> int:
    section("Sprint 1.2 — Auth (structure)")
    errs = 0
    router_path = ROOT / "app/auth/router.py"
    if router_path.exists():
        text = router_path.read_text()
        # Must have actual route handlers, not just comments
        if "@router.post" in text and "login" in text and "def " in text:
            ok("Auth router has login endpoint")
        else:
            fail("Auth router has no POST /auth/login implementation (stub only)")
            errs += 1
        if "@router.get" in text and "me" in text and "def " in text:
            ok("Auth router has /me endpoint")
        else:
            fail("Auth router has no GET /auth/me (stub only)")
            errs += 1
    return errs


# ── Sprint 1.3: RAG ──────────────────────────────────────────────────────────

def check_1_3_structure() -> int:
    section("Sprint 1.3 — RAG (structure)")
    errs = 0
    for path, desc in [
        ("app/rag/service.py", "RAG service"),
        ("app/rag/retriever.py", "Hybrid retriever"),
        ("app/rag/ingest.py", "Ingest pipeline"),
        ("app/api/library.py", "Library API"),
    ]:
        p = ROOT / path
        if not p.exists():
            fail(f"{path} missing")
            errs += 1
            continue
        text = p.read_text()
        if "stub" in text.lower() or "Full implementation" in text or len(text.strip()) < 200:
            fail(f"{path} — still stub (no real implementation)")
            errs += 1
        else:
            ok(f"{path} — {desc}")
    main = (ROOT / "app/main.py").read_text()
    if "library" in main and "include_router" in main:
        ok("main.py mounts library router")
    else:
        fail("main.py does not mount library router")
        errs += 1
    return errs


# ── Sprint 1.4: Agent ────────────────────────────────────────────────────────

def check_1_4_structure() -> int:
    section("Sprint 1.4 — Agent core (structure)")
    errs = 0
    core = ROOT / "app/agent/core.py"
    tools = ROOT / "app/agent/tools.py"
    chat = ROOT / "app/api/chat.py"
    if core.exists():
        t = core.read_text()
        if "run_agent" in t and "litellm" in t and "tool" in t:
            ok("app/agent/core.py — agent loop with tool handling")
        elif "stub" in t.lower():
            fail("app/agent/core.py — still stub")
            errs += 1
        else:
            ok("app/agent/core.py present")
    if tools.exists():
        t = tools.read_text()
        if "get_tools_for_user" in t or "search_library" in t:
            ok("app/agent/tools.py — tool registry / search_library")
        elif "stub" in t.lower():
            fail("app/agent/tools.py — still stub")
            errs += 1
    if chat.exists():
        t = chat.read_text()
        if "sessions" in t and "messages" in t and "stub" not in t.lower():
            ok("app/api/chat.py — chat API implemented")
        elif "stub" in t.lower():
            fail("app/api/chat.py — still stub")
            errs += 1
    main = (ROOT / "app/main.py").read_text()
    if "chat" in main and "include_router" in main:
        ok("main.py mounts chat router")
    else:
        fail("main.py does not mount chat router")
        errs += 1
    return errs


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify Sprint 1 functionality")
    ap.add_argument("--structure-only", action="store_true", help="Only run file/structure checks")
    ap.add_argument("--live-only", action="store_true", help="Only run live HTTP checks (server must be up)")
    args = ap.parse_args()

    total = 0
    if not args.live_only:
        total += check_1_1_structure()
        total += check_1_2_structure()
        total += check_1_3_structure()
        total += check_1_4_structure()
    if not args.structure_only:
        total += check_1_1_health_live()

    section("Summary")
    if total == 0:
        print("  All checks passed.")
    else:
        print(f"  {total} check(s) failed. See roadmap acceptance criteria for Sprint 1.1–1.4.")
    print()
    sys.exit(0 if total == 0 else 1)


if __name__ == "__main__":
    main()
