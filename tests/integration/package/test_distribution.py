import tomllib
from importlib.metadata import entry_points
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version
from wuf import __version__


def test_package_exposes_a_version() -> None:
    assert __version__


def test_distribution_exposes_canonical_and_compatibility_commands() -> None:
    scripts = {entry.name: entry.value for entry in entry_points(group="console_scripts")}

    assert scripts["wuf"] == "wuf.cli:main"
    assert scripts["workfold"] == "wuf.cli:main"


def test_repository_uses_the_flat_package_layout() -> None:
    repository = Path(__file__).resolve().parents[3]

    assert (repository / "wuf" / "__init__.py").is_file()
    assert not (repository / "src").exists()


def test_workfold_bridge_tracks_and_accepts_the_canonical_release() -> None:
    repository = Path(__file__).resolve().parents[3]
    canonical = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    bridge_path = repository / "compat" / "workfold" / "pyproject.toml"
    bridge = tomllib.loads(bridge_path.read_text(encoding="utf-8"))

    canonical_version = Version(canonical["project"]["version"])
    requirement = Requirement(bridge["project"]["dependencies"][0])
    assert bridge["project"]["version"] == canonical["project"]["version"]
    assert requirement.name == "wuf"
    assert str(requirement.specifier) == f"=={canonical_version}"
    assert "compat/workfold/pyproject.toml:project.version" in canonical["tool"]["semantic_release"]["version_toml"]
