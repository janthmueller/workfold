from __future__ import annotations

import re
from pathlib import Path

import pytest
from workfold.cli import build_parser, main

from support.git_repo import GitRepo


def test_cli_accepts_no_arguments_in_a_git_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = GitRepo.create(tmp_path / "repo")
    monkeypatch.chdir(repo.path)
    monkeypatch.setenv("TZ", "UTC")

    assert main([]) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("Time band")
    assert "No events in selected scope." in rendered
    assert rendered.index("Working hours") < rendered.index("Events")
    assert "Legend" not in rendered
    assert "Summary" not in rendered
    assert "Mo-Fr 08:00-16:30" in rendered
    assert "inside" in rendered.casefold()
    assert "outside" in rendered.casefold()
    assert not re.search(r"^(?:Scope|Period|Breakdown)\b", rendered, re.MULTILINE)
    assert not re.search(r"^Coverage\s{2,}", rendered, re.MULTILINE)
    assert "Coverage details:" not in rendered
    assert "Details\n" not in rendered
    assert "Workfold ·" not in rendered
    assert re.search(r"^Events\s+0$", rendered, re.MULTILINE)
    assert re.search(r"^Schedule\s+0 inside \(n/a\) · 0 outside \(n/a\)$", rendered, re.MULTILINE)
    assert re.search(r"^Calendar\s+0 weekday \(n/a\) · 0 weekend \(n/a\)$", rendered, re.MULTILINE)


def test_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out.startswith("workfold ")


def test_help_exposes_the_accuracy_and_privacy_notes() -> None:
    help_text = build_parser().format_help()

    assert "Accuracy notes:" in help_text
    assert "Reflogs are local, optional" in help_text
    assert "expiring." in help_text
    assert "ctime is metadata" in help_text
    assert "never contacts a remote" in help_text


def test_help_exposes_only_the_new_collection_grammar() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    normalized_help = " ".join(help_text.split())
    public_options = {option for action in parser._actions for option in action.option_strings}

    assert {"-t", "--time", "-m", "--mode", "-p", "--profile"} <= public_options
    assert "--time SELECTOR" in normalized_help
    assert "--mode {git,fs,all}" in normalized_help
    assert "--profile {standard,portable,full}" in normalized_help
    assert "standard (default)" in normalized_help
    assert "author, committer, and tagger" in normalized_help
    assert "no file changes or reflogs" in normalized_help
    assert "selected mode for full (not all time)" in normalized_help
    assert "implies coverage" not in normalized_help
    assert "--git-records KINDS" in help_text
    assert "--git-commit-times KINDS" in help_text
    assert "--git-commits-from {HEAD,all-local-refs}" in normalized_help
    assert "--fs-times KINDS" in help_text
    assert "--fs-entries KINDS" in help_text
    assert "show the detailed coverage ledger" in normalized_help
    assert {
        "--week",
        "--from",
        "--to",
        "--all",
        "--everything",
        "--source",
        "--git-mode",
        "--git-date",
        "--refs",
        "--portable",
        "--full",
        "--commit-times",
        "--commits-from",
        "--filesystem-times",
        "--filesystem-entries",
    }.isdisjoint(public_options)


@pytest.mark.parametrize(
    "option",
    [
        "--week",
        "--from",
        "--to",
        "--all",
        "--everything",
        "--source",
        "--git-mode",
        "--git-date",
        "--refs",
        "--portable",
        "--full",
        "--commit-times",
        "--commits-from",
        "--filesystem-times",
        "--filesystem-entries",
    ],
)
def test_cli_rejects_retired_collection_options(option: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([option])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "option",
    ["--prof", "--git-r", "--git-commit-t", "--git-commits-f", "--fs-e"],
)
def test_cli_rejects_undocumented_long_option_abbreviations(option: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([option])

    assert error.value.code == 2


def test_help_describes_the_cluster_window_duration() -> None:
    help_text = build_parser().format_help()
    normalized_help = " ".join(help_text.split())

    assert "--cluster-window DURATION" in help_text
    assert "default: 1h" in normalized_help
    assert "30s, 10m, '1h 5m'" in normalized_help
    assert "--bin" not in help_text


def test_cli_maps_usage_errors_to_status_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--timezone", "Not/A_Real_Zone"]) == 2

    captured = capsys.readouterr()
    assert not captured.out
    assert "unknown IANA timezone" in captured.err


def test_cli_escapes_control_characters_in_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--timezone", "Bad\n\x1b]8;;forged"]) == 2

    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert r"Bad\n\x1b]8;;forged" in captured.err
    assert captured.err.count("\n") == 1


def test_argparse_escapes_control_characters_in_unknown_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--\x1b[31mforged"])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert r"\x1b[31mforged" in captured.err


@pytest.mark.parametrize("selector", [("--time", "..9999-12-31"), ("--time", "9999-W52")])
def test_cli_rejects_date_selectors_without_a_representable_end(
    selector: tuple[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([*selector, "--timezone", "UTC"]) == 2
    assert "exclusive end" in capsys.readouterr().err


def test_cli_maps_local_calendar_boundary_overflow_to_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--mode", "fs", "--time", "0001-01-01..", "--timezone", "Asia/Kolkata"]) == 2
    assert "representable UTC" in capsys.readouterr().err


def test_cli_treats_a_closed_output_pipe_as_a_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def closed_pipe(*args: object, **kwargs: object) -> int:
        raise BrokenPipeError

    monkeypatch.setattr("workfold.application.run", closed_pipe)

    assert main([]) == 0
