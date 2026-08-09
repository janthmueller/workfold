from pathlib import Path

from workfold import __version__


def test_package_exposes_a_version() -> None:
    assert __version__


def test_repository_uses_the_flat_package_layout() -> None:
    repository = Path(__file__).resolve().parents[1]

    assert (repository / "workfold" / "__init__.py").is_file()
    assert not (repository / "src").exists()
