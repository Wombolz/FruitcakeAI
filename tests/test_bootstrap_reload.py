from pathlib import Path

from uvicorn import Config
from uvicorn.supervisors.watchfilesreload import FileFilter


def test_bootstrap_reload_excludes_runtime_directories_recursively(tmp_path):
    workspace = tmp_path / "workspace"
    storage = tmp_path / "storage"
    workspace.mkdir()
    storage.mkdir()

    config = Config(
        "app.main:app",
        reload=True,
        reload_dirs=[str(tmp_path)],
        reload_excludes=[str(workspace), str(storage)],
    )
    watched = FileFilter(config)

    source = tmp_path / "app" / "main.py"
    workspace_output = workspace / "7" / "reports" / "generated.py"
    storage_output = storage / "artifacts" / "generated.py"

    assert watched(source) is True
    assert watched(workspace_output) is False
    assert watched(storage_output) is False


def test_bootstrap_uses_absolute_runtime_directory_exclusions():
    script = Path("scripts/bootstrap.sh").read_text(encoding="utf-8")

    assert '--reload-exclude "$ROOT/workspace"' in script
    assert '--reload-exclude "$ROOT/storage"' in script
    assert "--reload-exclude 'workspace/*'" not in script
