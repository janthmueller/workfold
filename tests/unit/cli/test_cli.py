from __future__ import annotations

import os
import re
from io import BytesIO, TextIOWrapper
from pathlib import Path

import pytest
from workfold.application.errors import OperationalError
from workfold.cli import build_parser, configure_windows_stdio, main, parse_options

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


def test_truncated_missing_path_diagnostics_keep_the_usage_exit_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_paths = tuple(os.fspath(tmp_path / f"missing-{index}") for index in range(257))

    assert main([*missing_paths, "--mode", "fs", "--no-color"]) == 2
    captured = capsys.readouterr()
    assert "1 additional diagnostic(s) omitted" in captured.err
    assert "(errors=1, warnings=0, info=0)" in captured.err


@pytest.mark.parametrize(("platform_name", "expected_encoding"), [("win32", "utf-8"), ("linux", "cp1252")])
def test_cli_configures_utf8_output_only_on_windows(platform_name: str, expected_encoding: str) -> None:
    stream = TextIOWrapper(BytesIO(), encoding="cp1252", errors="strict")

    configure_windows_stdio((stream,), platform_name=platform_name)

    assert stream.encoding.casefold() == expected_encoding
    assert stream.errors == ("backslashreplace" if platform_name == "win32" else "strict")
    stream.close()


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
    actions_by_option = {option: action for action in parser._actions for option in action.option_strings}

    assert {
        "--config",
        "--no-config",
        "--show-config",
        "-t",
        "--time",
        "-m",
        "--mode",
        "-p",
        "--profile",
        "-e",
        "--events",
        "-l",
        "--list",
    } <= public_options
    assert "--time SELECTOR" in normalized_help
    assert "rolling duration such as 2w3d" in normalized_help
    assert "--mode {git,fs,both}" in normalized_help
    assert "--profile {standard,portable,full}" in normalized_help
    assert "evidence preset" in normalized_help
    assert "customizable low-noise defaults" in normalized_help
    assert "standard (built-in default)" in normalized_help
    assert "author, committer, and tagger" in normalized_help
    assert "no file changes or reflogs" in normalized_help
    assert "selected mode for full (not all time)" in normalized_help
    assert "implies coverage" not in normalized_help
    assert "--events SELECTOR [SELECTOR ...]" in normalized_help
    assert actions_by_option["-e"] is actions_by_option["--events"]
    assert "space-separated identifiers and '*' wildcards" in normalized_help
    assert "git:file-change:author" in normalized_help
    assert "fs:<file|directory|symlink>:<birth|modified|metadata-changed|accessed>" in normalized_help
    assert "Place PATH arguments before --events/--list" in normalized_help
    assert "after an option-terminating --" in normalized_help
    assert "--git-commits-from {head,local-branches,all-refs}" in normalized_help
    assert "standard built-in: local-branches; portable/full: all-refs" in normalized_help.replace("- ", "-")
    assert "--git-identity VALUE" in normalized_help
    assert "--marker-style {source,identity}" in normalized_help
    assert "--cluster-anchor {event,midnight}" in normalized_help
    assert "--band-label {range,start}" in normalized_help
    assert "--show-empty-bands" in normalized_help
    assert "--no-show-empty-bands" in normalized_help
    assert "compressed-gap threshold" in normalized_help
    assert "--grid {none,vertical,horizontal,both}" in normalized_help
    assert actions_by_option["-H"] is actions_by_option["--hide-days"]
    assert actions_by_option["-H"].metavar == "SCOPE"
    assert actions_by_option["-E"] is actions_by_option["--hide-empty-days"]
    assert actions_by_option["-E"].metavar == "SCOPE"
    assert "author, committer, tagger, or reflog identity" in normalized_help
    assert "--fs-entries" not in public_options
    assert "--fs-exclude PATTERN" in normalized_help
    assert "exclude root-relative filesystem paths using Git-style patterns" in normalized_help
    assert "allow automatic color, respecting terminal detection and NO_COLOR" in normalized_help
    assert "--list SELECTOR [SELECTOR ...]" in normalized_help
    assert actions_by_option["-l"] is actions_by_option["--list"]
    assert "details from already enabled report events" in normalized_help
    assert "selected by all, inside, outside, none, or event patterns" in normalized_help
    assert "maximum listed events (built-in default: 50; requires --list)" in normalized_help
    assert "show the detailed coverage ledger" in normalized_help
    assert "return non-zero when collection is incomplete" in normalized_help
    assert "scope and operational details plus the detailed coverage ledger" in normalized_help
    assert "Built-in defaults may be overridden" in normalized_help
    assert "--show-config to inspect effective values and origins" in normalized_help
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
        "--author",
        "--git-records",
        "--git-commit-times",
        "--fs-times",
        "--fs-entries",
        "--exclude",
        "--list-outside",
        "--no-list-outside",
    }.isdisjoint(public_options)


@pytest.mark.parametrize(
    "arguments",
    [
        ["repo", "--events", "git:commit:author"],
        ["--events", "git:commit:author", "--", "repo"],
        ["repo", "--list", "outside"],
        ["--list", "outside", "--", "repo"],
    ],
)
def test_space_separated_selectors_have_documented_path_disambiguation(arguments: list[str]) -> None:
    assert parse_options(arguments).paths == (Path("repo"),)


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
        "--author",
        "--git-records",
        "--git-commit-times",
        "--fs-times",
        "--fs-entries",
        "--exclude",
        "--list-outside",
        "--no-list-outside",
    ],
)
def test_cli_rejects_retired_collection_options(option: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([option])

    assert error.value.code == 2


def test_cli_rejects_retired_all_mode_value() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--mode", "all"])

    assert error.value.code == 2


@pytest.mark.parametrize("scope", ["HEAD", "all-local-refs"])
def test_cli_rejects_retired_commit_reachability_values(scope: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["--git-commits-from", scope])

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
    assert "built-in default: 1h" in normalized_help
    assert "1m30s, 10m, '1h 5m'" in normalized_help
    assert "--bin" not in help_text


def test_help_describes_all_hours_and_independent_band_controls() -> None:
    normalized_help = " ".join(build_parser().format_help().split())

    assert "all means every minute of all seven days" in normalized_help
    assert "fixed intervals from local midnight" in normalized_help
    assert "midnight requires whole-minute windows" in normalized_help
    assert "observed/fixed range (built-in default) or its starting minute" in normalized_help
    assert "explicitly clipped dense edges keep exact ranges" in normalized_help


def test_cli_maps_usage_errors_to_status_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--timezone", "Not/A_Real_Zone"]) == 2

    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.startswith("error: ")
    assert "workfold: error:" not in captured.err
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
    assert "\nerror: " in captured.err
    assert "workfold: error:" not in captured.err
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

    monkeypatch.setattr("workfold.cli.runner.run", closed_pipe)

    assert main([]) == 0


def test_cli_reports_operational_failures_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed(*args: object, **kwargs: object) -> int:
        raise OperationalError("temporary aggregation storage is unavailable")

    monkeypatch.setattr("workfold.cli.runner.run", failed)

    assert main([]) == 1
    assert capsys.readouterr().err == "error: temporary aggregation storage is unavailable\n"


def test_cli_maps_keyboard_interrupt_to_standard_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted(*args: object, **kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("workfold.cli.runner.run", interrupted)

    assert main([]) == 130
    assert capsys.readouterr().err == "interrupted\n"
