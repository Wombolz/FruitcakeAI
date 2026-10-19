"""Shared conservative source-inspection policy; not a filesystem grant."""

DENIED_PARTS = {".git", ".pi", ".codex", ".claude", ".agents", ".ssh", ".aws", ".gnupg",
    "node_modules", "vendor", "__pycache__", ".venv", "venv", "storage", "workspace", ".pytest_cache",
    "build", "dist", ".build", "deriveddata", "credentials", "secrets", "model_cache"}


def path_allowed(path):
    if not isinstance(path, str) or len(path.encode("utf-8")) > 1024 or any(ord(c) < 32 for c in path) or any(c in path for c in ('"', "\\")):
        return False
    parts = path.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") or part.casefold() in DENIED_PARTS for part in parts):
        return False
    name = parts[-1].casefold()
    return not (name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite", ".sqlite3", ".db"))
                or any(word in name for word in ("credential", "secret", "id_rsa", "id_ed25519"))
                or name in {"env", "config.json", "token.json", "tokens.json"})
