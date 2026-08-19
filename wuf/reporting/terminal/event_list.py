"""Bounded chronological event-detail rendering."""

from __future__ import annotations

from wuf.application.report import ListedEvent, Report
from wuf.configuration.options import ListSchedule
from wuf.domain.evidence import EvidenceKind
from wuf.domain.observations import GitChangeKind, RecordKind, RecordOrigin, TimestampKind
from wuf.reporting.sanitization import display_width, pad_right, sanitize_terminal_text, truncate_middle
from wuf.reporting.terminal.layout import fit_plain


def render_event_list(report: Report, width: int) -> str:
    """Render the requested bounded event projection."""

    title = _title(report)
    if not report.listed_events:
        return f"{title}\nNone"

    shown = len(report.listed_events)
    total = report.aggregation.listed_marker_count
    heading = f"{title} (showing {shown} of {total}"
    if report.aggregation.listed_omitted_count:
        heading += f"; {report.aggregation.listed_omitted_count} additional omitted"
    heading += ")"

    timestamp_width = 38
    schedule_width = max(len("Schedule"), len("outside"))
    show_schedule = report.event_list is None or report.event_list.schedule is ListSchedule.ALL
    column_heading = pad_right("Timestamp", timestamp_width)
    if show_schedule:
        column_heading += f" {pad_right('Schedule', schedule_width)}"
    lines = [fit_plain(heading, width), column_heading.rstrip()]
    for event in report.listed_events:
        lines.extend(
            _event_lines(
                event,
                width,
                timestamp_width,
                schedule_width,
                show_schedule=show_schedule,
            )
        )
    return "\n".join(fit_plain(line, width) for line in lines)


def _title(report: Report) -> str:
    selection = report.event_list
    if selection is None or selection.schedule is ListSchedule.ALL:
        return "Events"
    if selection.schedule is ListSchedule.INSIDE:
        return "Events inside working hours"
    return "Events outside working hours"


def _event_lines(
    event: ListedEvent,
    width: int,
    timestamp_width: int,
    schedule_width: int,
    *,
    show_schedule: bool,
) -> tuple[str, ...]:
    timestamp = _format_exact_local_timestamp(event)
    schedule = "inside" if event.within_schedule else "outside"
    primary = pad_right(sanitize_terminal_text(timestamp), timestamp_width)
    if show_schedule:
        primary += f" {pad_right(schedule, schedule_width)}"
    primary = primary.rstrip()

    origin = event.origin
    parts = [_evidence_label(origin, event.timestamp_roles)]
    identity = _display_identity(origin)
    if identity is not None:
        parts.append(identity)
    lines = [primary, _indented(" · ".join(parts), width)]
    lines.append(_indented(str(origin.repository_or_root), width))
    description = _event_description(origin)
    if description:
        lines.append(_indented(description, width))
    return tuple(lines)


def _indented(value: str, width: int) -> str:
    prefix = "  "
    available = max(1, width - display_width(prefix))
    return prefix + truncate_middle(sanitize_terminal_text(value), available)


def _display_identity(origin: RecordOrigin) -> str | None:
    if origin.commit_id is not None:
        return origin.commit_id[:10]
    return origin.ref_name


def _evidence_label(origin: RecordOrigin, roles: tuple[TimestampKind, ...]) -> str:
    identifiers = tuple(
        EvidenceKind.from_dimensions(origin.record_kind, role, origin.entry_type).value for role in roles
    )
    if len(identifiers) == 1:
        return identifiers[0]
    prefixes = tuple(identifier.rsplit(":", maxsplit=1)[0] for identifier in identifiers)
    if len(set(prefixes)) == 1:
        suffixes = ",".join(identifier.rsplit(":", maxsplit=1)[1] for identifier in identifiers)
        return f"{prefixes[0]}:{{{suffixes}}}"
    return "+".join(identifiers)


def _format_exact_local_timestamp(event: ListedEvent) -> str:
    local = event.local_datetime
    offset = local.utcoffset()
    if offset is None:
        raise ValueError("listed event timestamps must be timezone-aware")
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


def _event_description(origin: RecordOrigin) -> str | None:
    """Format source-aware detail while retaining structured provenance."""

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


__all__ = ["render_event_list"]
