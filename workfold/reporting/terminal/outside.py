"""Bounded chronological outside-hours event table."""

from __future__ import annotations

from workfold.application.report import OutsideEvent, Report
from workfold.domain.observations import GitChangeKind, RecordKind, RecordOrigin, Source, TimestampKind
from workfold.reporting.sanitization import (
    display_width,
    pad_right,
    sanitize_terminal_text,
    truncate_end,
    truncate_middle,
)
from workfold.reporting.terminal.layout import fit_plain


def render_outside(report: Report, width: int) -> str:
    aggregation = report.aggregation
    if not report.outside_events:
        return "Outside-hours events\nNone"

    shown = len(report.outside_events)
    total = aggregation.outside_marker_count
    heading = f"Outside-hours events (showing {shown} of {total}"
    if aggregation.outside_omitted_count:
        heading += f"; {aggregation.outside_omitted_count} older omitted"
    heading += ")"

    timestamp_width = 38
    role_width = 18
    identity_width = 12
    fixed_width = timestamp_width + role_width + identity_width + 3
    detail_width = max(1, width - fixed_width)
    lines = [
        fit_plain(heading, width),
        (
            f"{pad_right('Timestamp', timestamp_width)} "
            f"{pad_right('Source/role', role_width)} "
            f"{pad_right('Identity', identity_width)} "
            f"{truncate_end('Repository/root and detail', detail_width)}"
        ),
    ]
    for event in report.outside_events:
        lines.extend(_outside_lines(event, detail_width, width))
    return "\n".join(fit_plain(line, width) for line in lines)


def _outside_lines(event: OutsideEvent, detail_width: int, width: int) -> tuple[str, ...]:
    timestamp = _format_exact_local_timestamp(event)
    origin = event.origin
    source_role = f"{_source_short_label(origin.source)}/{_roles_label(event.timestamp_roles)}"
    identity = (
        origin.commit_id[:10]
        if origin.commit_id is not None
        else (origin.ref_name if origin.ref_name is not None else event.provenance_id[:10])
    )
    detail_parts = [str(origin.repository_or_root)]
    if origin.commit_id is not None and origin.ref_name is not None:
        detail_parts.append(f"[{origin.ref_name}]")
    detail = " | ".join(sanitize_terminal_text(part) for part in detail_parts)
    primary = (
        f"{pad_right(sanitize_terminal_text(timestamp), 38)} "
        f"{pad_right(sanitize_terminal_text(source_role), 18)} "
        f"{pad_right(sanitize_terminal_text(identity), 12)} "
        f"{truncate_middle(detail, detail_width)}"
    )
    description = _event_description(origin)
    if not description:
        return (primary,)

    description_prefix = "  Detail: "
    description = truncate_end(
        sanitize_terminal_text(description),
        width - display_width(description_prefix),
    )
    return primary, description_prefix + description


def _format_exact_local_timestamp(event: OutsideEvent) -> str:
    local = event.local_datetime
    offset = local.utcoffset()
    if offset is None:
        raise ValueError("outside-event timestamps must be timezone-aware")
    total_offset_seconds = int(offset.total_seconds())
    sign = "+" if total_offset_seconds >= 0 else "-"
    absolute_offset = abs(total_offset_seconds)
    hours, remainder = divmod(absolute_offset, 3_600)
    minutes, seconds = divmod(remainder, 60)
    offset_text = f"{sign}{hours:02d}:{minutes:02d}"
    if seconds:
        offset_text += f":{seconds:02d}"

    nanoseconds = event.occurred_at_utc_ns % 1_000_000_000
    fraction = f".{nanoseconds:09d}" if nanoseconds else ""
    return local.strftime("%Y-%m-%dT%H:%M:%S") + fraction + offset_text


def _source_short_label(source: Source) -> str:
    return "git" if source is Source.GIT else "fs"


def _roles_label(roles: tuple[TimestampKind, ...]) -> str:
    return "+".join(role.value.removeprefix("git_").removeprefix("fs_") for role in roles)


def _event_description(origin: RecordOrigin) -> str | None:
    """Format source-aware detail while retaining structured report provenance."""

    if origin.record_kind is RecordKind.GIT_FILE_CHANGE:
        change = origin.change_kind.value if origin.change_kind is not None else GitChangeKind.OTHER.value
        if origin.old_path is not None and origin.path is not None:
            path_detail = f"{origin.old_path} -> {origin.path}"
        elif origin.path is not None:
            path_detail = str(origin.path)
        else:
            path_detail = "unknown path"
        file_detail = f"{change}: {path_detail}"
        return f"{file_detail} | {origin.description}" if origin.description else file_detail
    if origin.description is not None:
        return origin.description
    if origin.old_path is not None and origin.path is not None:
        return f"{origin.old_path} -> {origin.path}"
    if origin.path is not None:
        return str(origin.path)
    return None
