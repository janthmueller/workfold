"""Shared policies for assigning and labeling wall-clock time bands."""

from __future__ import annotations

from datetime import timedelta
from enum import Enum


class ClusterAnchor(str, Enum):
    """How occupied events are assigned to half-open time bands."""

    EVENT = "event"
    MIDNIGHT = "midnight"


def duration_nanoseconds(value: timedelta) -> int:
    """Convert a duration to an exact integral nanosecond count."""

    return (value.days * 86_400 + value.seconds) * 1_000_000_000 + value.microseconds * 1_000


def validate_cluster_anchor(value: object) -> ClusterAnchor:
    """Return a validated cluster anchor for public aggregation APIs."""

    if not isinstance(value, ClusterAnchor):
        raise TypeError("cluster_anchor must be a ClusterAnchor")
    return value


def validate_cluster_window_alignment(cluster_window: timedelta, cluster_anchor: ClusterAnchor) -> None:
    """Validate constraints introduced by a band's wall-clock anchor."""

    if cluster_anchor is ClusterAnchor.MIDNIGHT and cluster_window % timedelta(minutes=1):
        raise ValueError("midnight-anchored cluster_window must use whole minutes")


__all__ = [
    "ClusterAnchor",
    "duration_nanoseconds",
    "validate_cluster_anchor",
    "validate_cluster_window_alignment",
]
