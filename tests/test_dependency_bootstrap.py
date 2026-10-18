"""Exercise installer safety without touching the real virtualenv or pip."""

from pathlib import Path
import hashlib
import os
import subprocess

def _bootstrap(tmp_path, *, retired=False, check_failure=False, stamped=False):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path("scripts/_install_common.sh").read_text()
    (scripts / "_install_common.sh").write_text(source)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0 --hash=sha256:abc\n")
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text(f'export PATH="{bin_dir}:$PATH"\n')
    python = bin_dir / "python"
    python.write_text("""#!/usr/bin/env bash
set -eu
if [[ "$1" == "-" ]]; then
  cat >/dev/null
  [[ "$HAS_RETIRED" == "1" ]] && exit 0
  exit 1
fi
printf '%s\\n' "$*" >> "$CALL_LOG"
if [[ "$*" == "-m pip check" && "$CHECK_FAILURE" == "1" ]]; then
  exit 1
fi
""")
    python.chmod(0o755)
    stamp = tmp_path / ".venv" / ".fruitcake-requirements.sha256"
    if stamped:
        stamp.write_text(hashlib.sha256(requirements.read_bytes()).hexdigest() + "\n")
    log = tmp_path / "calls.log"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; ensure_venv', "bash", str(scripts / "_install_common.sh")],
        env={**os.environ, "HAS_RETIRED": str(int(retired)),
             "CHECK_FAILURE": str(int(check_failure)), "CALL_LOG": str(log)},
        capture_output=True, text=True,
    )
    return result, stamp, log.read_text() if log.exists() else ""


def test_bootstrap_checks_hashes_wheels_and_consistency_before_stamping(tmp_path):
    result, stamp, calls = _bootstrap(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--require-hashes --only-binary=:all:" in calls
    assert calls.splitlines()[-1] == "-m pip check"
    assert stamp.exists()


def test_bootstrap_refuses_to_leave_retired_packages_in_place(tmp_path):
    result, stamp, calls = _bootstrap(tmp_path, retired=True)
    assert result.returncode != 0
    assert "retired authentication packages" in result.stderr
    assert not stamp.exists()
    assert calls == ""


def test_bootstrap_does_not_stamp_failed_consistency_check(tmp_path):
    result, stamp, _ = _bootstrap(tmp_path, check_failure=True)
    assert result.returncode != 0
    assert not stamp.exists()


def test_unchanged_baseline_does_not_reinstall(tmp_path):
    result, stamp, calls = _bootstrap(tmp_path, stamped=True)
    assert result.returncode == 0, result.stderr
    assert stamp.exists()
    assert calls == ""
