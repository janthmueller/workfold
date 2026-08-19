"""Cross-option validation and event-profile expansion."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from wuf.configuration.options import (
    DEFAULT_CLUSTER_WINDOW,
    BandLabel,
    CountGrouping,
    DisplayHours,
    EventListSelection,
    GridStyle,
    MarkerStyle,
    RefScope,
    RollingDuration,
    RunOptions,
    TerminalPreferences,
    UnresolvedOptions,
    UsageError,
)
from wuf.configuration.parsing import (
    parse_cluster_window,
    parse_display_hours,
    parse_event_list,
    parse_event_selectors,
    parse_time_selectors,
    parse_weekday_scopes,
    validate_cluster_options,
)
from wuf.configuration.profiles import EventProfile, evidence_for_profile
from wuf.domain.evidence import EvidenceKind, EvidenceSelection
from wuf.domain.observations import RecordKind, Source, Weekday
from wuf.domain.schedule import parse_schedule
from wuf.domain.time import resolve_timezone
from wuf.folding.bands import ClusterAnchor

_Parsed = TypeVar("_Parsed")


def _explicit(values: UnresolvedOptions, name: str) -> bool:
    return name in values.explicit_names


def resolve_options(values: UnresolvedOptions) -> RunOptions:
    """Validate typed setting values and expand named event profiles."""

    weeks, from_date, to_date, all_dates, rolling_duration = _parse_settings(
        ("time",),
        lambda: parse_time_selectors(values.time_selectors),
    )
    schedule = _parse_settings(("hours",), lambda: parse_schedule(values.hours))
    timezone_name = values.timezone_name
    timezone_value = _parse_settings(("timezone",), lambda: resolve_timezone(timezone_name)) if timezone_name else None
    profiles = values.profiles
    if len(profiles) > 1:
        raise UsageError("--profile may be supplied only once", setting_keys=("profile",))
    selected_profile = _parse_settings(
        ("profile",),
        lambda: EventProfile(profiles[0] if profiles else EventProfile.GIT.value),
    )

    if values.event_selectors is not None:
        event_selectors = values.event_selectors
        if _explicit(values, "profiles"):
            raise UsageError(
                "--events cannot be combined with --profile",
                setting_keys=("events", "profile"),
            )
        evidence = _parse_settings(("events",), lambda: parse_event_selectors(event_selectors))
        profile = None
    else:
        evidence = evidence_for_profile(selected_profile)
        profile = selected_profile

    if values.commits_from is not None:
        ref_scope = {
            "head": RefScope.HEAD,
            "local-branches": RefScope.LOCAL_BRANCHES,
            "all-refs": RefScope.ALL_REFS,
        }[values.commits_from]
    else:
        ref_scope = RefScope.LOCAL_BRANCHES

    includes_git = evidence.includes_source(Source.GIT)
    includes_filesystem = evidence.includes_source(Source.FILESYSTEM)
    selection_keys = ("events",) if profile is None else ("profile",)
    explicit_git = _explicit(values, "commits_from") or bool(values.git_identities)
    explicit_filesystem = _explicit(values, "include_ignored") or bool(values.exclusions)
    if not includes_git and explicit_git:
        git_keys = (
            *(("git-commits-from",) if _explicit(values, "commits_from") else ()),
            *(("git-identity",) if values.git_identities else ()),
        )
        raise UsageError(
            "Git-specific options require at least one Git event",
            setting_keys=(*git_keys, *selection_keys),
        )
    if not includes_filesystem and explicit_filesystem:
        filesystem_keys = (
            *(("include-ignored",) if _explicit(values, "include_ignored") else ()),
            *(("fs-exclude",) if values.exclusions else ()),
        )
        raise UsageError(
            "filesystem-specific options require at least one filesystem event",
            setting_keys=(*filesystem_keys, *selection_keys),
        )

    has_commit_records = any(
        kind.record_kind in {RecordKind.COMMIT, RecordKind.GIT_FILE_CHANGE} for kind in evidence.kinds
    )
    if _explicit(values, "commits_from") and not has_commit_records:
        raise UsageError(
            "--git-commits-from requires commit or file-change events to be enabled",
            setting_keys=("git-commits-from", *selection_keys),
        )

    git_identities = tuple(value.strip() for value in values.git_identities)
    if any(not value for value in git_identities):
        raise UsageError("--git-identity values cannot be empty", setting_keys=("git-identity",))
    exclusions = tuple(value.strip() for value in values.exclusions)
    if any(not value for value in exclusions):
        raise UsageError("--fs-exclude values cannot be empty", setting_keys=("fs-exclude",))
    if any(value.startswith("!") for value in exclusions):
        raise UsageError(
            "--fs-exclude patterns cannot be negated; explicit exclusions always win",
            setting_keys=("fs-exclude",),
        )

    list_selectors = values.list_selectors
    event_list = _parse_settings(("list",), lambda: parse_event_list(list_selectors)) if list_selectors else None
    if event_list is not None and event_list.evidence_kinds:
        assert values.list_selectors is not None
        event_list = _resolve_list_selection(values.list_selectors, event_list, evidence)
    if _explicit(values, "limit") and event_list is None:
        raise UsageError("--limit requires --list", setting_keys=("limit", "list"))
    limit = values.limit
    if limit < 1:
        raise UsageError("--limit must be at least 1", setting_keys=("limit",))

    include_ignored = values.include_ignored
    respect_gitignore = not include_ignored

    hide_days = _parse_settings(
        ("hide-days",),
        lambda: parse_weekday_scopes(values.hide_days, option="--hide-days"),
    )
    if hide_days == tuple(Weekday):
        raise UsageError("--hide-days cannot hide all seven weekday columns", setting_keys=("hide-days",))
    hide_empty_days = _parse_settings(
        ("hide-empty-days",),
        lambda: parse_weekday_scopes(values.hide_empty_days, option="--hide-empty-days"),
    )

    cluster_window = _parse_settings(
        ("cluster-window",),
        lambda: parse_cluster_window(values.cluster_window),
    )
    cluster_anchor = _parse_settings(
        ("cluster-anchor",),
        lambda: ClusterAnchor(values.cluster_anchor),
    )
    _parse_settings(
        ("cluster-window", "cluster-anchor", "show-empty-bands"),
        lambda: validate_cluster_options(
            cluster_window,
            cluster_anchor,
            show_empty_bands=values.show_empty_bands,
        ),
        include_values=True,
    )
    display_hours_text = values.display_hours
    display_hours = (
        _parse_settings(("display-hours",), lambda: parse_display_hours(display_hours_text))
        if display_hours_text
        else None
    )

    paths = values.paths or (Path("."),)
    return RunOptions(
        paths=paths,
        weeks=weeks,
        from_date=from_date,
        to_date=to_date,
        all_dates=all_dates,
        rolling_duration=rolling_duration,
        profile=profile,
        evidence=evidence,
        ref_scope=ref_scope,
        git_identities=git_identities,
        include_ignored=include_ignored,
        respect_gitignore=respect_gitignore,
        exclusions=exclusions,
        schedule=schedule,
        timezone=timezone_value,
        cluster_window=cluster_window,
        cluster_anchor=cluster_anchor,
        display_hours=display_hours,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
        terminal=TerminalPreferences(
            marker_style=MarkerStyle(values.marker_style),
            count_grouping=CountGrouping(values.count_grouping),
            grid_style=GridStyle(values.grid_style),
            band_label=BandLabel(values.band_label),
            show_empty_bands=values.show_empty_bands,
            no_color=values.no_color,
            event_list=event_list,
            event_limit=limit,
            coverage=values.coverage,
            strict=values.strict,
            verbose=values.verbose,
        ),
    )


def _parse_settings(
    keys: tuple[str, ...],
    operation: Callable[[], _Parsed],
    *,
    include_values: bool = False,
) -> _Parsed:
    """Parse one effective value and retain its setting provenance on failure."""

    try:
        return operation()
    except UsageError as error:
        if error.setting_keys:
            raise
        raise UsageError(str(error), setting_keys=keys, include_setting_values=include_values) from error
    except ValueError as error:
        raise UsageError(str(error), setting_keys=keys, include_setting_values=include_values) from error


def _resolve_list_selection(
    raw_values: tuple[str, ...],
    parsed: EventListSelection,
    enabled: EvidenceSelection,
) -> EventListSelection:
    """Match each list event selector only against the enabled report scope."""

    enabled_kinds = frozenset(enabled.kinds)
    selected: set[EvidenceKind] = set()
    event_selectors = tuple(
        value.strip().lower() for value in raw_values if value.strip().lower() not in {"all", "inside", "outside"}
    )
    for selector in event_selectors:
        expanded = parse_event_selectors((selector,), option="--list")
        matches = enabled_kinds.intersection(expanded.kinds)
        if not matches:
            raise UsageError(
                f"--list selector {selector!r} matches no event kind enabled in the report; "
                "enable matching events with --events or an appropriate profile",
                setting_keys=("list",),
            )
        selected.update(matches)
    return EventListSelection(
        schedule=parsed.schedule,
        evidence_kinds=tuple(kind for kind in EvidenceKind if kind in selected),
    )


__all__ = [
    "DEFAULT_CLUSTER_WINDOW",
    "BandLabel",
    "ClusterAnchor",
    "DisplayHours",
    "GridStyle",
    "EventProfile",
    "MarkerStyle",
    "RunOptions",
    "RefScope",
    "RollingDuration",
    "TerminalPreferences",
    "UnresolvedOptions",
    "UsageError",
    "parse_cluster_window",
    "parse_display_hours",
    "parse_event_list",
    "parse_event_selectors",
    "parse_time_selectors",
    "parse_weekday_scopes",
    "resolve_options",
]
