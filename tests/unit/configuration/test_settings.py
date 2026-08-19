from __future__ import annotations

import re
from pathlib import Path

import pytest
from workfold.cli import main, parse_invocation, parse_options
from workfold.cli.config_display import format_resolved_settings
from workfold.configuration import (
    BandLabel,
    ClusterAnchor,
    CollectionProfile,
    EventListSelection,
    GridStyle,
    ListSchedule,
    OriginKind,
    SourceMode,
    UsageError,
    global_config_path,
)
from workfold.configuration.schema import DEFAULT_SETTINGS, SETTING_BY_DESTINATION, SETTING_BY_KEY, SETTING_SPECS
from workfold.domain.evidence import EvidenceKind, EvidenceSelection, evidence_mask
from workfold.reporting.sanitization import display_width


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


def test_setting_schema_is_the_complete_ordered_config_and_cli_catalog() -> None:
    assert tuple(DEFAULT_SETTINGS) == tuple(spec.key for spec in SETTING_SPECS)
    assert tuple(SETTING_BY_KEY) == tuple(DEFAULT_SETTINGS)
    assert set(SETTING_BY_DESTINATION) == {spec.cli_destination for spec in SETTING_SPECS}
    assert all(spec.default == DEFAULT_SETTINGS[spec.key] for spec in SETTING_SPECS)


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
        'timezone = "Europe/Berlin"\nhours = "Mo-Fr 09:00-17:00"\ncluster-anchor = "midnight"\ngrid = "vertical"\n',
    )
    local_path = _write(
        project / "workfold.toml",
        'hours = "Mo-Thu 08:00-16:00"\nband-label = "start"\nhide-empty-days = ["weekend"]\n',
    )

    invocation = parse_invocation(
        [str(child), "--grid", "both"],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.timezone is not None and invocation.options.timezone.key == "Europe/Berlin"
    assert str(invocation.options.schedule) == "Mo-Th 08:00-16:00"
    assert invocation.options.cluster_anchor is ClusterAnchor.MIDNIGHT
    assert invocation.options.terminal.band_label is BandLabel.START
    assert invocation.options.terminal.grid_style is GridStyle.BOTH
    assert invocation.settings.global_config == global_path
    assert invocation.settings.local_config == local_path
    assert invocation.settings.origins["timezone"].kind is OriginKind.GLOBAL
    assert invocation.settings.origins["hours"].kind is OriginKind.LOCAL
    assert invocation.settings.origins["cluster-anchor"].kind is OriginKind.GLOBAL
    assert invocation.settings.origins["band-label"].kind is OriginKind.LOCAL
    assert invocation.settings.origins["grid"].kind is OriginKind.CLI


def test_global_and_local_event_styles_merge_by_property_and_selector(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    global_path = global_config_path(environ=environ, platform_name="linux")
    _write(
        global_path,
        '[styles."git:*"]\ncolor = "yellow"\noutside-color = "magenta"\n',
    )
    local_path = _write(
        project / "workfold.toml",
        '[styles."git:tag:*"]\nsymbol = "◆"\noutside-symbol = "◇"\n',
    )

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    tag = invocation.options.terminal.event_styles.style_for(evidence_mask((EvidenceKind.GIT_TAG_TAGGER,)))
    commit = invocation.options.terminal.event_styles.style_for(evidence_mask((EvidenceKind.GIT_COMMIT_AUTHOR,)))
    assert (tag.inside.symbol, tag.inside.color) == ("◆", "yellow")
    assert (tag.outside.symbol, tag.outside.color) == ("◇", "magenta")
    assert (commit.inside.symbol, commit.inside.color) == ("●", "yellow")
    assert [layer.origin.path for layer in invocation.settings.style_layers] == [
        global_path.resolve(),
        local_path.resolve(),
    ]


def test_pyproject_accepts_nested_event_style_tables(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / "pyproject.toml",
        '[tool.workfold.styles."fs:file:modified"]\nsymbol = "M"\ncolor = "cyan"\n',
    )

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    visual = invocation.options.terminal.event_styles.style_for(evidence_mask((EvidenceKind.FS_FILE_MODIFIED,)))
    assert (visual.inside.symbol, visual.inside.color) == ("M", "cyan")


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

    assert invocation.options.timezone is not None and invocation.options.timezone.key == "UTC"
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

    assert invocation.options.timezone is not None and invocation.options.timezone.key == "Europe/Berlin"
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

    assert invocation.options.timezone is not None and invocation.options.timezone.key == "UTC"
    assert invocation.settings.local_config == parent_config.resolve()


def test_malformed_pyproject_event_style_table_is_an_actionable_error(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / "pyproject.toml",
        '[tool.workfold.styles."git:*"]\nsymbol = [\n',
    )

    with pytest.raises(UsageError, match="invalid TOML"):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


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

    assert invocation.options.terminal.grid_style is GridStyle.VERTICAL
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

    assert invocation.options.terminal.grid_style is GridStyle.NONE
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

    assert invocation.options.terminal.grid_style is GridStyle.BOTH
    assert invocation.settings.config_disabled
    assert invocation.settings.origins["grid"].kind is OriginKind.CLI
    assert invocation.settings.origins["timezone"].kind is OriginKind.BUILTIN


def test_internal_option_parser_is_isolated_from_developer_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "workfold.toml", 'grid = "vertical"\n')
    monkeypatch.chdir(tmp_path)

    assert parse_options([]).terminal.grid_style is GridStyle.NONE


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

    assert invocation.options.timezone is None
    assert invocation.options.display_hours is None
    assert invocation.settings.origins["timezone"].kind is OriginKind.LOCAL
    assert invocation.settings.origins["display-hours"].kind is OriginKind.LOCAL

    cli = parse_invocation(
        ["--no-config", "--timezone", "local", "--display-hours", "auto"],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )
    assert cli.options.timezone is None
    assert cli.options.display_hours is None
    assert cli.settings.origins["timezone"].kind is OriginKind.CLI


def test_higher_precedence_locked_profile_replaces_lower_scope_details(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        global_config_path(environ=environ, platform_name="linux"),
        'events = ["git:file-change:committer"]\n',
    )
    _write(project / "workfold.toml", 'profile = "portable"\n')

    options = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert options.profile is CollectionProfile.PORTABLE
    assert options.evidence == EvidenceSelection.create(
        (
            EvidenceKind.GIT_COMMIT_AUTHOR,
            EvidenceKind.GIT_COMMIT_COMMITTER,
            EvidenceKind.GIT_TAG_TAGGER,
        )
    )


@pytest.mark.parametrize(
    ("global_setting", "local_mode", "expected_source"),
    [
        ('git-identity = ["global@example.test"]\n', "fs", SourceMode.FILESYSTEM),
        ('fs-exclude = ["*.log"]\n', "git", SourceMode.GIT),
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


def test_higher_precedence_profile_replaces_lower_custom_source_details(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        global_config_path(environ=environ, platform_name="linux"),
        'events = ["fs:file:modified"]\ninclude-ignored = true\nfs-exclude = ["*.log"]\n',
    )
    _write(project / "workfold.toml", 'profile = "standard"\n')

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.source is SourceMode.GIT
    assert invocation.options.exclusions == ()
    assert not invocation.options.include_ignored
    assert invocation.effective.origins["fs-exclude"].label == ("built-in mode + local profile (source selection)")
    rendered = " ".join(format_resolved_settings(invocation.settings, invocation.effective).split())
    assert "fs-exclude [] built-in mode + local profile (source selection)" in rendered


def test_show_config_attributes_preset_expansion_to_mode_and_profile(tmp_path: Path) -> None:
    invocation = parse_invocation(
        ["--no-config", "--mode", "fs"],
        cwd=tmp_path,
        environ=_environment(tmp_path),
        platform_name="linux",
    )

    output = format_resolved_settings(invocation.settings, invocation.effective)
    normalized = " ".join(output.split())

    assert "events [fs:file:birth, fs:file:modified] CLI mode + built-in profile (preset expansion)" in normalized


def test_show_config_wraps_full_event_selection_to_terminal_width(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("TZ", "UTC")

    assert main(["--no-config", "--events", "*", "--show-config"]) == 0
    output = capsys.readouterr().out

    assert max(display_width(line) for line in output.splitlines()) <= 80
    assert "git:commit:author" in output
    assert "fs:symlink:accessed" in output


@pytest.mark.parametrize(
    ("global_settings", "local_events", "expected_source"),
    [
        (
            'mode = "fs"\nfs-exclude = ["*.log"]\n',
            'events = ["git:tag:tagger"]\n',
            SourceMode.GIT,
        ),
        (
            'mode = "git"\ngit-identity = ["global@example.test"]\n',
            'events = ["fs:file:modified"]\n',
            SourceMode.FILESYSTEM,
        ),
    ],
)
def test_higher_precedence_custom_events_discard_lower_disabled_collector_settings(
    tmp_path: Path,
    global_settings: str,
    local_events: str,
    expected_source: SourceMode,
) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(global_config_path(environ=environ, platform_name="linux"), global_settings)
    _write(project / "workfold.toml", local_events)

    options = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert options.profile is CollectionProfile.CUSTOM
    assert options.source is expected_source
    assert options.git_identities == ()
    assert options.exclusions == ()


def test_higher_precedence_non_commit_events_discard_lower_commit_reachability(
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        global_config_path(environ=environ, platform_name="linux"),
        'git-commits-from = "all-refs"\n',
    )
    _write(project / "workfold.toml", 'events = ["git:tag:tagger"]\n')

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.evidence == EvidenceSelection.create((EvidenceKind.GIT_TAG_TAGGER,))
    assert invocation.options.ref_scope.value == "local-branches"
    assert invocation.settings.origins["events"].kind is OriginKind.LOCAL


def test_mode_profile_and_custom_events_cannot_share_one_config_layer(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = _write(project / "workfold.toml", 'mode = "git"\nevents = ["git:tag:tagger"]\n')

    with pytest.raises(UsageError, match=rf"{re.escape(str(config))}.*same precedence layer"):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_invalid_selection_family_in_lower_layer_cannot_be_hidden_by_partial_override(
    tmp_path: Path,
) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    global_path = _write(
        global_config_path(environ=environ, platform_name="linux"),
        'mode = "fs"\nevents = ["git:tag:tagger"]\n',
    )
    _write(project / "workfold.toml", 'profile = "standard"\n')

    with pytest.raises(UsageError, match=rf"{re.escape(str(global_path))}.*same precedence layer"):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_incompatible_inherited_list_selector_identifies_its_config_file(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    global_path = _write(
        global_config_path(environ=environ, platform_name="linux"),
        'list = ["git:commit:author"]\n',
    )
    _write(project / "workfold.toml", 'events = ["fs:file:modified"]\n')

    expected = rf"{re.escape(str(global_path))}: list: .*matches no event kind enabled"
    with pytest.raises(UsageError, match=expected):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_combined_mode_is_supported_in_configuration(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "workfold.toml", 'mode = "both"\n')

    options = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert options.source is SourceMode.BOTH


def test_retired_all_mode_is_rejected_in_configuration(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "workfold.toml", 'mode = "all"\n')

    with pytest.raises(UsageError, match="must be one of both, fs, git"):
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )


def test_locked_profile_and_scope_details_in_one_layer_are_rejected(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    config = _write(project / "workfold.toml", 'profile = "portable"\ngit-commits-from = "head"\n')

    with pytest.raises(UsageError) as captured:
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )

    message = str(captured.value)
    assert message.startswith(f"{config.resolve()}: profile, git-commits-from:")
    assert "--profile portable controls --git-commits-from" in message


def test_cross_setting_error_identifies_origins_from_distinct_config_layers(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    global_path = global_config_path(environ=environ, platform_name="linux")
    _write(global_path, 'mode = "fs"\n')
    local_path = _write(project / "workfold.toml", 'git-commits-from = "head"\n')

    with pytest.raises(UsageError) as captured:
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )

    message = str(captured.value)
    assert message.startswith("Git-specific options cannot be used with --mode fs")
    assert f"git-commits-from (local {local_path.resolve()})" in message
    assert f"mode (global {global_path.resolve()})" in message
    assert "profile (built-in)" in message


def test_cli_can_negate_booleans_and_disable_a_configured_event_list(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    _write(
        project / "workfold.toml",
        'no-color = true\nlist = ["outside"]\nlimit = 7\ncoverage = true\nstrict = true\nverbose = true\n',
    )

    configured = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options
    assert configured.terminal.event_list == EventListSelection(schedule=ListSchedule.OUTSIDE)
    assert configured.terminal.event_limit == 7

    options = parse_invocation(
        [
            str(project),
            "--color",
            "--list",
            "none",
            "--no-coverage",
            "--no-strict",
            "--no-verbose",
        ],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    ).options

    assert not options.terminal.no_color
    assert options.terminal.event_list is None
    assert options.terminal.event_limit == 7
    assert not options.terminal.coverage
    assert not options.terminal.strict
    assert not options.terminal.verbose


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('unknown-setting = "value"\n', "unknown Workfold configuration key"),
        ('coverage = "yes"\n', "must be true or false"),
        ('events = "git:commit:author"\n', "must be an array of strings"),
        ('mode = "remote"\n', "must be one of"),
        ('cluster-anchor = "noon"\n', "must be one of"),
        ('band-label = "compact"\n', "must be one of"),
        ('show-empty-bands = "yes"\n', "must be true or false"),
        ('[styles."git:*"]\nsymbol = "XX"\n', "one printable terminal cell"),
        ('[styles."git:*"]\ncolor = "not-a-color"\n', "valid terminal color"),
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


def test_midnight_anchor_error_identifies_values_from_distinct_config_layers(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    global_path = global_config_path(environ=environ, platform_name="linux")
    _write(global_path, 'cluster-anchor = "midnight"\n')
    local_path = _write(project / "workfold.toml", 'cluster-window = "1m30s"\n')

    with pytest.raises(UsageError) as captured:
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )

    message = str(captured.value)
    assert "midnight" in message
    assert "whole minutes" in message
    assert "HH:MM" in message
    assert f"cluster-anchor=midnight (global {global_path.resolve()})" in message
    assert f"cluster-window=1m30s (local {local_path.resolve()})" in message


def test_show_empty_bands_conflict_identifies_config_origins(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    local_path = _write(project / "workfold.toml", "show-empty-bands = true\n")

    with pytest.raises(UsageError) as captured:
        parse_invocation(
            [str(project)],
            cwd=tmp_path,
            environ=environ,
            platform_name="linux",
        )

    message = str(captured.value)
    assert "--show-empty-bands requires --cluster-anchor midnight" in message
    assert f"show-empty-bands=true (local {local_path.resolve()})" in message
    assert "cluster-anchor=event (built-in)" in message


def test_configured_show_empty_bands_is_valid_with_midnight_anchor(tmp_path: Path) -> None:
    environ = _environment(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    local_path = _write(
        project / "workfold.toml",
        'cluster-anchor = "midnight"\nshow-empty-bands = true\n',
    )

    invocation = parse_invocation(
        [str(project)],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )

    assert invocation.options.terminal.show_empty_bands
    assert invocation.settings.origins["show-empty-bands"].path == local_path.resolve()

    overridden = parse_invocation(
        [str(project), "--cluster-anchor", "event", "--no-show-empty-bands"],
        cwd=tmp_path,
        environ=environ,
        platform_name="linux",
    )
    assert not overridden.options.terminal.show_empty_bands
    assert overridden.options.cluster_anchor is ClusterAnchor.EVENT
    assert overridden.settings.origins["show-empty-bands"].kind is OriginKind.CLI


def test_show_config_prints_values_origins_and_files_without_collecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    local = _write(
        project / "workfold.toml",
        'timezone = "UTC"\ngrid = "vertical"\n\n[styles."git:tag:*"]\nsymbol = "◆"\ncolor = "magenta"\n',
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TZ", "UTC")

    assert (
        main(
            [
                "--show-config",
                "--grid",
                "both",
                "--cluster-anchor",
                "midnight",
                "--band-label",
                "start",
                "--show-empty-bands",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Configuration files" in output
    assert f"Local{local.resolve()}" in "".join(output.split())
    assert "Effective value" in output
    assert "timezone" in output and "UTC" in output and "local" in output
    assert "grid" in output and "both" in output and "CLI" in output
    assert re.search(r"^cluster-anchor\s+midnight\s+CLI$", output, re.MULTILINE)
    assert re.search(r"^band-label\s+start\s+CLI$", output, re.MULTILINE)
    assert re.search(r"^show-empty-bands\s+true\s+CLI$", output, re.MULTILINE)
    assert "Event styles" in output
    assert re.search(r"^git:tag:\*\s+symbol=◆ color=magenta\s+local$", output, re.MULTILINE)
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
        'events = ["fs:file:modified"]\ntime = "all"\ntimezone = "UTC"\nno-color = true\n\n'
        '[styles."fs:file:modified"]\nsymbol = "M"\ncolor = "cyan"\n',
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("TZ", "UTC")

    assert main([]) == 0

    output = capsys.readouterr().out
    assert "M Filesystem files" in output
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
