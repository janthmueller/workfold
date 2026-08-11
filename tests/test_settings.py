from __future__ import annotations

import re
from pathlib import Path

import pytest
from workfold.cli import main, parse_invocation, parse_options
from workfold.config import CollectionProfile, GitDateMode, GitMode, GitRecords, GridStyle, SourceMode, UsageError
from workfold.settings import OriginKind, global_config_path


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "TZ": "UTC",
    }


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("platform_name", "environ", "expected"),
    [
        (
            "linux",
            {"HOME": "/home/ada", "XDG_CONFIG_HOME": "/var/config"},
            Path("/var/config/workfold/workfold.toml"),
        ),
        (
            "linux",
            {"HOME": "/home/ada", "XDG_CONFIG_HOME": "relative"},
            Path("/home/ada/.config/workfold/workfold.toml"),
        ),
        (
            "darwin",
            {"HOME": "/Users/ada"},
            Path("/Users/ada/Library/Application Support/workfold/workfold.toml"),
        ),
        (
            "win32",
            {"USERPROFILE": "C:/Users/Ada", "APPDATA": "C:/Users/Ada/AppData/Roaming"},
            Path("C:/Users/Ada/AppData/Roaming/workfold/workfold.toml"),
        ),
    ],
)
def test_global_config_path_uses_platform_conventions(
    platform_name: str,
    environ: dict[str, str],
    expected: Path,
) -> None:
    assert global_config_path(environ=environ, platform_name=platform_name) == expected


def test_global_local_and_cli_layers_merge_by_key(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    child = project / "nested"
    child.mkdir(parents=True)
    global_path = global_config_path(environ=environ, platform_name="linux")
    _write(
        global_path,
        'timezone = "Europe/Berlin"\nhours = "Mo-Fr 09:00-17:00"\ngrid = "vertical"\n',
    )
    local_path = _write(
        project / "workfold.toml",
        'hours = "Mo-Thu 08:00-16:00"\nhide-empty-days = ["weekend"]\n',
    )

    invocation = parse_invocation(
        [str(child), "--grid", "both"],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.timezone_name == "Europe/Berlin"
    assert invocation.options.hours == "Mo-Thu 08:00-16:00"
    assert invocation.options.grid_style is GridStyle.BOTH
    assert invocation.settings.global_config == global_path
    assert invocation.settings.local_config == local_path
    assert invocation.settings.origins["timezone"].kind is OriginKind.GLOBAL
    assert invocation.settings.origins["hours"].kind is OriginKind.LOCAL
    assert invocation.settings.origins["grid"].kind is OriginKind.CLI


def test_nearest_pyproject_table_is_a_local_config(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    child = project / "src" / "package"
    child.mkdir(parents=True)
    pyproject = _write(
        project / "pyproject.toml",
        '[project]\nname = "fixture"\n\n[tool.workfold]\ntimezone = "UTC"\ncluster-window = "15m"\n',
    )

    invocation = parse_invocation(
        [str(child)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.timezone_name == "UTC"
    assert invocation.options.cluster_window.total_seconds() == 900
    assert invocation.settings.local_config == pyproject.resolve()


def test_valid_quoted_pyproject_table_is_discovered_semantically(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    pyproject = _write(
        project / "pyproject.toml",
        '[project]\nname = "fixture"\n\n[tool."workfold"]\ntimezone = "Europe/Berlin"\n',
    )

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.timezone_name == "Europe/Berlin"
    assert invocation.settings.local_config == pyproject.resolve()


def test_unrelated_malformed_pyproject_does_not_become_a_config(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    parent = tmp_path / "parent"
    project = parent / "project"
    project.mkdir(parents=True)
    parent_config = _write(parent / "workfold.toml", 'timezone = "UTC"\n')
    _write(project / "pyproject.toml", "this is not valid TOML = [\n")

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.timezone_name == "UTC"
    assert invocation.settings.local_config == parent_config.resolve()


def test_standalone_config_wins_over_pyproject_in_the_same_directory(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "pyproject.toml", '[tool.workfold]\ngrid = "horizontal"\n')
    standalone = _write(project / "workfold.toml", 'grid = "vertical"\n')

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.grid_style is GridStyle.VERTICAL
    assert invocation.settings.local_config == standalone.resolve()


def test_multiple_paths_must_resolve_to_the_same_local_config(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write(first / "workfold.toml", 'timezone = "UTC"\n')
    _write(second / "workfold.toml", 'timezone = "Europe/Berlin"\n')

    with pytest.raises(UsageError, match="different local Workfold configurations"):
        parse_invocation(
            [str(first), str(second)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_explicit_config_replaces_automatic_discovery(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(global_config_path(environ=environ, platform_name="linux"), 'grid = "horizontal"\n')
    _write(project / "workfold.toml", 'grid = "vertical"\n')
    explicit = _write(tmp_path / "chosen.toml", 'grid = "none"\ntimezone = "UTC"\n')

    invocation = parse_invocation(
        [str(project), "--config", str(explicit)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.grid_style is GridStyle.NONE
    assert invocation.settings.explicit_config == explicit.resolve()
    assert invocation.settings.global_config is None
    assert invocation.settings.local_config is None
    assert invocation.settings.origins["grid"].kind is OriginKind.EXPLICIT


def test_no_config_uses_only_builtins_and_cli(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(global_config_path(environ=environ, platform_name="linux"), 'grid = "horizontal"\n')
    _write(project / "workfold.toml", 'grid = "vertical"\n')

    invocation = parse_invocation(
        [str(project), "--no-config", "--grid", "both"],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.grid_style is GridStyle.BOTH
    assert invocation.settings.config_disabled
    assert invocation.settings.origins["grid"].kind is OriginKind.CLI
    assert invocation.settings.origins["timezone"].kind is OriginKind.BUILTIN


def test_internal_option_parser_is_isolated_from_developer_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "workfold.toml", 'grid = "vertical"\n')
    monkeypatch.chdir(tmp_path)

    assert parse_options([]).grid_style is GridStyle.NONE


def test_local_array_replaces_and_can_clear_a_global_array(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        global_config_path(environ=environ, platform_name="linux"),
        'git-identity = ["global@example.test"]\nhide-empty-days = ["weekend"]\n',
    )
    _write(project / "workfold.toml", 'git-identity = []\nhide-empty-days = ["all"]\n')

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.git_identities == ()
    assert len(invocation.options.hide_empty_days) == 7
    assert invocation.settings.origins["git-identity"].kind is OriginKind.LOCAL


def test_local_sentinels_can_restore_dynamic_defaults(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        global_config_path(environ=environ, platform_name="linux"),
        'timezone = "Europe/Berlin"\ndisplay-hours = "06:00-22:00"\n',
    )
    _write(project / "workfold.toml", 'timezone = "local"\ndisplay-hours = "auto"\n')

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.timezone_name is None
    assert invocation.options.display_hours is None
    assert invocation.settings.origins["timezone"].kind is OriginKind.LOCAL
    assert invocation.settings.origins["display-hours"].kind is OriginKind.LOCAL

    cli = parse_invocation(
        ["--no-config", "--timezone", "local", "--display-hours", "auto"],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )
    assert cli.options.timezone_name is None
    assert cli.options.display_hours is None
    assert cli.settings.origins["timezone"].kind is OriginKind.CLI


def test_higher_precedence_locked_profile_replaces_lower_scope_details(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        global_config_path(environ=environ, platform_name="linux"),
        'git-records = ["file-change"]\ngit-commit-times = ["committer"]\n',
    )
    _write(project / "workfold.toml", 'profile = "portable"\n')

    options = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert options.profile is CollectionProfile.PORTABLE
    assert options.git_mode is GitMode.COMMITS
    assert options.git_date is GitDateMode.BOTH
    assert options.git_records == GitRecords.COMMITS | GitRecords.TAGS


@pytest.mark.parametrize(
    ("global_setting", "local_mode", "expected_source"),
    [
        ('git-identity = ["global@example.test"]\n', "fs", SourceMode.FILESYSTEM),
        ('exclude = ["*.log"]\n', "git", SourceMode.GIT),
    ],
)
def test_higher_precedence_mode_discards_lower_disabled_collector_settings(
    tmp_path: Path,
    global_setting: str,
    local_mode: str,
    expected_source: SourceMode,
) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(global_config_path(environ=environ, platform_name="linux"), global_setting)
    _write(project / "workfold.toml", f'mode = "{local_mode}"\n')

    options = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert options.source is expected_source
    assert options.git_identities == ()
    assert options.exclusions == ()


def test_locked_profile_and_scope_details_in_one_layer_are_rejected(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "workfold.toml", 'profile = "portable"\ngit-records = ["commit"]\n')

    with pytest.raises(UsageError, match="--profile portable controls --git-records"):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_cli_can_negate_configured_boolean_values(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / "workfold.toml",
        "no-color = true\nlist-outside = true\nlimit = 7\ncoverage = true\nstrict = true\nverbose = true\n",
    )

    configured = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options
    assert configured.list_outside
    assert configured.limit == 7

    options = parse_invocation(
        [
            str(project),
            "--color",
            "--no-list-outside",
            "--no-coverage",
            "--no-strict",
            "--no-verbose",
        ],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert not options.no_color
    assert not options.list_outside
    assert options.limit == 7
    assert not options.coverage
    assert not options.strict
    assert not options.verbose


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('unknown-setting = "value"\n', "unknown Workfold configuration key"),
        ('coverage = "yes"\n', "must be true or false"),
        ('git-records = "commit"\n', "must be an array of strings"),
        ('mode = "remote"\n', "must be one of"),
        ("hours = [\n", "invalid TOML"),
    ],
)
def test_invalid_configuration_is_an_actionable_usage_error(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "workfold.toml", content)

    with pytest.raises(UsageError, match=message):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_domain_value_errors_identify_the_config_file_and_key(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = _write(project / "workfold.toml", 'hours = "not a schedule"\n')

    expected = rf"{re.escape(str(config.resolve()))}: hours:"
    with pytest.raises(UsageError, match=expected):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_show_config_prints_values_origins_and_files_without_collecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    local = _write(project / "workfold.toml", 'timezone = "UTC"\ngrid = "vertical"\n')
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TZ", "UTC")

    assert main(["--show-config", "--grid", "both"]) == 0

    output = capsys.readouterr().out
    assert "Configuration files" in output
    assert f"Local   {local.resolve()}" in output
    assert "Effective value" in output
    assert "timezone" in output and "UTC" in output and "local" in output
    assert "grid" in output and "both" in output and "CLI" in output
    assert "Time band" not in output


def test_normal_cli_run_applies_project_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / "workfold.toml",
        'mode = "fs"\ntime = "all"\nfs-times = ["modified"]\ntimezone = "UTC"\nno-color = true\n',
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TZ", "UTC")

    assert main([]) == 0

    output = capsys.readouterr().out
    assert "Filesystem" in output
    assert "Events" in output


def test_explicit_pyproject_requires_a_workfold_table(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    pyproject = _write(tmp_path / "pyproject.toml", '[project]\nname = "fixture"\n')

    with pytest.raises(UsageError, match=r"no \[tool.workfold\] table"):
        parse_invocation(
            ["--config", str(pyproject)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )
