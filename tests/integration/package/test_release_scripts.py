from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

SynchronizeVersion = Callable[[Path, str], None]


def _synchronizer() -> SynchronizeVersion:
    namespace = runpy.run_path(str(Path(__file__).parents[3] / ".github/scripts/sync_lock_version.py"))
    return cast(SynchronizeVersion, namespace["synchronize_version"])


def _write_release_files(repository: Path, *, project_version: str, locked_version: str) -> None:
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "workfold"\nversion = "{project_version}"\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text(
        """version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "dependency"
version = "9.9.9"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "workfold"
version = """
        + f'"{locked_version}"\n'
        + 'source = { editable = "." }\n',
        encoding="utf-8",
    )


def test_release_version_sync_updates_only_the_editable_project_block(tmp_path: Path) -> None:
    _write_release_files(tmp_path, project_version="0.1.0-alpha.3", locked_version="0.1.0a2")

    _synchronizer()(tmp_path, "0.1.0-alpha.3")

    contents = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "dependency"\nversion = "9.9.9"' in contents
    assert 'name = "workfold"\nversion = "0.1.0a3"' in contents
    assert "0.1.0a2" not in contents


def test_release_version_sync_rejects_a_version_not_stamped_in_pyproject(tmp_path: Path) -> None:
    _write_release_files(tmp_path, project_version="0.1.0-alpha.3", locked_version="0.1.0a2")

    with pytest.raises(ValueError, match="does not match pyproject"):
        _synchronizer()(tmp_path, "0.1.0-alpha.4")
