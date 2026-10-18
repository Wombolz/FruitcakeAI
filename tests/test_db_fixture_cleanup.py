"""The workflow smoke subprocess must exit, not just print passing assertions."""
from pathlib import Path
import subprocess
import sys


def test_auth_smoke_subprocess_exits_after_database_fixture_cleanup():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_auth.py", "-k",
         "create_and_list_sessions or order"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert " passed" in result.stdout
