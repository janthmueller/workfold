"""Cross-option validation and collection-profile expansion."""

from __future__ import annotations

from pathlib import Path

from workfold.configuration.options import (
    DEFAULT_CLUSTER_WINDOW,
    DEFAULT_HOURS,
    BandLabel,
    CollectionProfile,
    DisplayHours,
    EventListSelection,
    GridStyle,
    MarkerStyle,
    RefScope,
    RollingDuration,
    RunOptions,
    SourceMode,
    TerminalPreferences,
    UnresolvedOptions,
    UsageError,
)
from workfold.configuration.parsing import (
    parse_clock_minutes,
    parse_cluster_window,
    parse_display_hours,
    parse_event_list,
    parse_event_selectors,
    parse_rolling_duration,
    parse_time_selectors,
    parse_weekday_scopes,
    validate_cluster_options,
    validate_iso_week,
)
from workfold.domain.evidence import EvidenceKind, EvidenceSelection
from workfold.domain.observations import RecordKind, Source, Weekday
from workfold.folding.bands import ClusterAnchor


def _explicit(values: UnresolvedOptions, name: str) -> bool:
    return name in values.explicit_names


def resolve_options(values: UnresolvedOptions) -> RunOptions:
    """Validate typed setting values and expand evidence presets."""

    weeks, from_date, to_date, all_dates, rolling_duration = parse_time_selectors(values.time_selectors)
    modes = values.modes
    if len(modes) > 1:
        raise UsageError("--mode may be supplied only once", setting_keys=("mode",))
    mode = modes[0] if modes else "git"
    profiles = values.profiles
    if len(profiles) > 1:
        raise UsageError("--profile may be supplied only once", setting_keys=("profile",))
    selected_profile = CollectionProfile(profiles[0] if profiles else CollectionProfile.STANDARD.value)
    selected_source = {
        "git": SourceMode.GIT,
        "fs": SourceMode.FILESYSTEM,
        "both": SourceMode.BOTH,
    }[mode]

    if values.event_selectors is not None:
        if _explicit(values, "modes") or _explicit(values, "profiles"):
            preset_keys = (
                *(("mode",) if _explicit(values, "modes") else ()),
                *(("profile",) if _explicit(values, "profiles") else ()),
            )
            raise UsageError(
                "--events cannot be combined with --mode or --profile",
                setting_keys=("events", *preset_keys),
            )
        evidence = parse_event_selectors(values.event_selectors)
        profile = CollectionProfile.CUSTOM
    else:
        if selected_profile is CollectionProfile.PORTABLE and selected_source is not SourceMode.GIT:
            raise UsageError(
                "--profile portable is valid only with --mode git",
                setting_keys=("profile", "mode"),
            )
        evidence = _preset_evidence(selected_source, selected_profile)
        profile = selected_profile

    source = _source_mode(evidence)
    preset = None if profile in {CollectionProfile.STANDARD, CollectionProfile.CUSTOM} else f"--profile {profile.value}"
    controlled = {
        "--git-commits-from": ("commits_from", "git-commits-from"),
        "--include-ignored/--respect-gitignore": ("include_ignored", "include-ignored"),
    }
    if preset is not None:
        conflicts = [flag for flag, (name, _key) in controlled.items() if _explicit(values, name)]
        if conflicts:
            conflict_keys = tuple(key for _flag, (name, key) in controlled.items() if _explicit(values, name))
            raise UsageError(
                f"{preset} controls {', '.join(conflicts)}; remove the scope option(s)",
                setting_keys=("profile", *conflict_keys),
            )

    if values.commits_from is not None:
        ref_scope = {
            "head": RefScope.HEAD,
            "local-branches": RefScope.LOCAL_BRANCHES,
            "all-refs": RefScope.ALL_REFS,
        }[values.commits_from]
    elif profile in {CollectionProfile.PORTABLE, CollectionProfile.FULL}:
        ref_scope = RefScope.ALL_REFS
    else:
        ref_scope = RefScope.LOCAL_BRANCHES

    explicit_git = _explicit(values, "commits_from") or bool(values.git_identities)
    explicit_filesystem = _explicit(values, "include_ignored") or bool(values.exclusions)
    if source is SourceMode.FILESYSTEM and explicit_git:
        suffix = (
            "with --mode fs" if profile is not CollectionProfile.CUSTOM else "when only filesystem events are selected"
        )
        git_keys = (
            *(("git-commits-from",) if _explicit(values, "commits_from") else ()),
            *(("git-identity",) if values.git_identities else ()),
        )
        source_keys = ("events",) if profile is CollectionProfile.CUSTOM else ("mode", "profile")
        raise UsageError(
            f"Git-specific options cannot be used {suffix}",
            setting_keys=(*git_keys, *source_keys),
        )
    if source is SourceMode.GIT and explicit_filesystem:
        suffix = "with --mode git" if profile is not CollectionProfile.CUSTOM else "when only Git events are selected"
        filesystem_keys = (
            *(("include-ignored",) if _explicit(values, "include_ignored") else ()),
            *(("fs-exclude",) if values.exclusions else ()),
        )
        source_keys = ("events",) if profile is CollectionProfile.CUSTOM else ("mode", "profile")
        raise UsageError(
            f"filesystem-specific options cannot be used {suffix}",
            setting_keys=(*filesystem_keys, *source_keys),
        )

    has_commit_records = any(
        kind.record_kind in {RecordKind.COMMIT, RecordKind.GIT_FILE_CHANGE} for kind in evidence.kinds
    )
    if _explicit(values, "commits_from") and not has_commit_records:
        evidence_keys = ("events",) if profile is CollectionProfile.CUSTOM else ("mode", "profile")
        raise UsageError(
            "--git-commits-from requires commit or file-change events to be enabled",
            setting_keys=("git-commits-from", *evidence_keys),
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

    event_list = parse_event_list(values.list_selectors) if values.list_selectors is not None else None
    if event_list is not None and event_list.evidence_kinds:
        assert values.list_selectors is not None
        event_list = _resolve_list_selection(values.list_selectors, event_list, evidence)
    if _explicit(values, "limit") and event_list is None:
        raise UsageError("--limit requires --list", setting_keys=("limit", "list"))
    limit = values.limit
    if limit < 1:
        raise UsageError("--limit must be at least 1", setting_keys=("limit",))

    include_ignored = bool((profile is CollectionProfile.FULL and source.includes_filesystem) or values.include_ignored)
    respect_gitignore = not include_ignored

    hide_days = parse_weekday_scopes(values.hide_days, option="--hide-days")
    if hide_days == tuple(Weekday):
        raise UsageError("--hide-days cannot hide all seven weekday columns", setting_keys=("hide-days",))
    hide_empty_days = parse_weekday_scopes(values.hide_empty_days, option="--hide-empty-days")

    cluster_window = parse_cluster_window(values.cluster_window)
    cluster_anchor = ClusterAnchor(values.cluster_anchor)
    validate_cluster_options(
        cluster_window,
        cluster_anchor,
        show_empty_bands=values.show_empty_bands,
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
        hours=values.hours or DEFAULT_HOURS,
        timezone_name=values.timezone_name,
        cluster_window=cluster_window,
        cluster_anchor=cluster_anchor,
        display_hours=parse_display_hours(values.display_hours) if values.display_hours else None,
        hide_days=hide_days,
        hide_empty_days=hide_empty_days,
        terminal=TerminalPreferences(
            marker_style=MarkerStyle(values.marker_style),
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


def _preset_evidence(source: SourceMode, profile: CollectionProfile) -> EvidenceSelection:
    if profile is CollectionProfile.PORTABLE:
        return EvidenceSelection.create(
            (
                EvidenceKind.GIT_COMMIT_AUTHOR,
                EvidenceKind.GIT_COMMIT_COMMITTER,
                EvidenceKind.GIT_TAG_TAGGER,
            )
        )
    if profile is CollectionProfile.FULL:
        return EvidenceSelection.create(
            kind
            for kind in EvidenceKind
            if (source.includes_git and kind.source is Source.GIT)
            or (source.includes_filesystem and kind.source is Source.FILESYSTEM)
        )

    standard: list[EvidenceKind] = []
    if source.includes_git:
        standard.append(EvidenceKind.GIT_COMMIT_AUTHOR)
    if source.includes_filesystem:
        standard.extend((EvidenceKind.FS_FILE_BIRTH, EvidenceKind.FS_FILE_MODIFIED))
    return EvidenceSelection.create(standard)


def _source_mode(evidence: EvidenceSelection) -> SourceMode:
    includes_git = evidence.includes_source(Source.GIT)
    includes_filesystem = evidence.includes_source(Source.FILESYSTEM)
    if includes_git and includes_filesystem:
        return SourceMode.BOTH
    return SourceMode.GIT if includes_git else SourceMode.FILESYSTEM


def _resolve_list_selection(
    raw_values: tuple[str, ...],
    parsed: EventListSelection,
    enabled: EvidenceSelection,
) -> EventListSelection:
    """Match each list event selector only against the enabled report scope."""

    enabled_kinds = frozenset(enabled.kinds)
    selected: set[EvidenceKind] = set()
    event_selectors = tuple(
        value.strip().lower() for value in raw_values if value.strip().lower() not in {"inside", "outside"}
    )
    for selector in event_selectors:
        expanded = parse_event_selectors((selector,), option="--list")
        matches = enabled_kinds.intersection(expanded.kinds)
        if not matches:
            raise UsageError(
                f"--list selector {selector!r} matches no event kind enabled in the report; "
                "enable matching events with --events or an appropriate mode/profile",
                setting_keys=("list",),
            )
        selected.update(matches)
    return EventListSelection(
        schedule=parsed.schedule,
        evidence_kinds=tuple(kind for kind in EvidenceKind if kind in selected),
    )


__all__ = [
    "DEFAULT_CLUSTER_WINDOW",
    "DEFAULT_HOURS",
    "BandLabel",
    "ClusterAnchor",
    "CollectionProfile",
    "DisplayHours",
    "GridStyle",
    "MarkerStyle",
    "RunOptions",
    "RefScope",
    "RollingDuration",
    "SourceMode",
    "TerminalPreferences",
    "UnresolvedOptions",
    "UsageError",
    "parse_clock_minutes",
    "parse_cluster_window",
    "parse_display_hours",
    "parse_event_list",
    "parse_event_selectors",
    "parse_rolling_duration",
    "parse_time_selectors",
    "parse_weekday_scopes",
    "resolve_options",
    "validate_iso_week",
]
