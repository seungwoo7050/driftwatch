"""driftwatch: Periodic fact drift detector and debounce alert engine."""

from driftwatch.app import create_app
from driftwatch.extractor import (
    HttpFetcher,
    extract_value,
    validate_extracted_value,
)
from driftwatch.models import (
    AlertRecord,
    Base,
    CheckResult,
    CheckSnapshotRow,
    DriftAlertRow,
    DriftSettings,
    TargetCreateRequest,
    TargetSnapshot,
    WatchTargetRow,
)
from driftwatch.runner import DriftRunner, MetricsTracker
from driftwatch.store import DriftStore


def main(argv: list[str] | None = None) -> None:
    """CLI application entrypoint."""
    from driftwatch.cli import main as _main

    _main(argv)


__all__ = [
    "AlertRecord",
    "Base",
    "CheckResult",
    "CheckSnapshotRow",
    "DriftAlertRow",
    "DriftRunner",
    "DriftSettings",
    "DriftStore",
    "HttpFetcher",
    "MetricsTracker",
    "TargetCreateRequest",
    "TargetSnapshot",
    "WatchTargetRow",
    "create_app",
    "extract_value",
    "main",
    "validate_extracted_value",
]
