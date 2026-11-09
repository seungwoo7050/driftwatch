"""SQLAlchemy 2.0 persistence layer with thread-safe session helper and debounced state transitions."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from driftwatch.models import (
    AlertRecord,
    Base,
    CheckSnapshotRow,
    DriftAlertRow,
    TargetCreateRequest,
    TargetSnapshot,
    WatchTargetRow,
)

T = TypeVar("T")


class DriftStore:
    """Storage engine managing targets, check snapshots, and debounced alert records."""

    def __init__(self, db_url: str = "sqlite:///:memory:") -> None:
        self.db_url = db_url
        engine_kwargs: dict[str, Any] = {}
        if ":memory:" in db_url or "sqlite" in db_url:
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in db_url:
            engine_kwargs["poolclass"] = StaticPool

        self._engine = create_engine(db_url, **engine_kwargs)
        self._session_factory = sessionmaker(
            bind=self._engine,
            autoflush=False,
            expire_on_commit=False,
        )
        self._lock = threading.Lock()
        self.create_tables()

    def create_tables(self) -> None:
        """Create all tables defined in metadata."""
        with self._lock:
            Base.metadata.create_all(self._engine)

    async def _execute(self, fn: Callable[[Session], T]) -> T:
        """Execute a database operation with session in a worker thread."""
        def _wrapper() -> T:
            with self._lock, self._session_factory() as session:
                return fn(session)

        return await asyncio.to_thread(_wrapper)

    async def upsert_target(self, req: TargetCreateRequest) -> TargetSnapshot:
        """Insert or update target configuration."""
        def _op(session: Session) -> TargetSnapshot:
            target = session.get(WatchTargetRow, req.target_id)
            if target is None:
                target = WatchTargetRow(
                    **req.model_dump(),
                    last_value=None,
                    consecutive_failures=0,
                    status="HEALTHY",
                    last_checked_at=None,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(target)
            else:
                for field, val in req.model_dump().items():
                    if field != "target_id":
                        setattr(target, field, val)
            session.commit()
            return TargetSnapshot.model_validate(target)

        return await self._execute(_op)

    async def get_target(self, target_id: str) -> TargetSnapshot | None:
        """Retrieve target snapshot by id."""
        def _op(session: Session) -> TargetSnapshot | None:
            target = session.get(WatchTargetRow, target_id)
            return TargetSnapshot.model_validate(target) if target else None

        return await self._execute(_op)

    async def list_targets(self) -> list[TargetSnapshot]:
        """List all registered targets ordered by creation time."""
        def _op(session: Session) -> list[TargetSnapshot]:
            rows = session.scalars(
                select(WatchTargetRow).order_by(WatchTargetRow.created_at.asc())
            ).all()
            return [TargetSnapshot.model_validate(r) for r in rows]

        return await self._execute(_op)

    async def list_alerts(
        self,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[AlertRecord]:
        """List recent alerts, optionally filtered by target."""
        def _op(session: Session) -> list[AlertRecord]:
            stmt = select(DriftAlertRow).order_by(DriftAlertRow.created_at.desc()).limit(limit)
            if target_id is not None:
                stmt = stmt.where(DriftAlertRow.target_id == target_id)
            rows = session.scalars(stmt).all()
            return [AlertRecord.model_validate(r) for r in rows]

        return await self._execute(_op)

    async def mark_alert_delivered(self, alert_id: int) -> bool:
        """Mark an alert record as delivered via webhook."""
        def _op(session: Session) -> bool:
            alert = session.get(DriftAlertRow, alert_id)
            if alert is None:
                return False
            alert.webhook_delivered = True
            session.commit()
            return True

        return await self._execute(_op)

    async def record_check_success(
        self,
        target_id: str,
        normalized_value: str,
        rule_violation_msg: str | None,
    ) -> tuple[TargetSnapshot, AlertRecord | None]:
        """Record successful check, evaluating diff and resetting consecutive failures."""
        def _op(session: Session) -> tuple[TargetSnapshot, AlertRecord | None]:
            target = session.get(WatchTargetRow, target_id)
            if target is None:
                raise ValueError(f"Target '{target_id}' not found")

            now = datetime.now(timezone.utc)
            target.last_checked_at = now
            target.consecutive_failures = 0
            alert_created: DriftAlertRow | None = None
            prev_value = target.last_value

            if rule_violation_msg is not None:
                target.status = "DRIFTED"
                target.last_value = normalized_value
                alert_created = DriftAlertRow(
                    target_id=target_id,
                    alert_type="RULE_VIOLATION",
                    previous_value=prev_value,
                    current_value=normalized_value,
                    message=rule_violation_msg,
                    webhook_delivered=False,
                    created_at=now,
                )
                session.add(alert_created)
            elif prev_value is not None and prev_value != normalized_value:
                target.status = "DRIFTED"
                target.last_value = normalized_value
                alert_created = DriftAlertRow(
                    target_id=target_id,
                    alert_type="VALUE_DRIFT",
                    previous_value=prev_value,
                    current_value=normalized_value,
                    message=f"Value shifted from '{prev_value}' to '{normalized_value}'",
                    webhook_delivered=False,
                    created_at=now,
                )
                session.add(alert_created)
            else:
                target.status = "HEALTHY"
                target.last_value = normalized_value

            session.add(
                CheckSnapshotRow(
                    target_id=target_id,
                    extracted_value=normalized_value,
                    status=target.status,
                    error_detail=rule_violation_msg,
                    checked_at=now,
                )
            )
            session.commit()
            return (
                TargetSnapshot.model_validate(target),
                AlertRecord.model_validate(alert_created) if alert_created else None,
            )

        return await self._execute(_op)

    async def record_check_failure(
        self,
        target_id: str,
        error_detail: str,
    ) -> tuple[TargetSnapshot, AlertRecord | None]:
        """Record check failure, applying N-threshold debounce before emitting alert."""
        def _op(session: Session) -> tuple[TargetSnapshot, AlertRecord | None]:
            target = session.get(WatchTargetRow, target_id)
            if target is None:
                raise ValueError(f"Target '{target_id}' not found")

            now = datetime.now(timezone.utc)
            target.last_checked_at = now
            target.consecutive_failures += 1
            alert_created: DriftAlertRow | None = None

            if target.consecutive_failures < target.failure_threshold:
                target.status = "DEGRADED"
            elif target.consecutive_failures == target.failure_threshold:
                target.status = "FAILING"
                alert_created = DriftAlertRow(
                    target_id=target_id,
                    alert_type="CONSECUTIVE_FAILURES",
                    previous_value=target.last_value,
                    current_value=None,
                    message=(
                        f"Consecutive check failures reached debounce threshold "
                        f"({target.failure_threshold}): {error_detail}"
                    ),
                    webhook_delivered=False,
                    created_at=now,
                )
                session.add(alert_created)
            else:
                target.status = "FAILING"

            session.add(
                CheckSnapshotRow(
                    target_id=target_id,
                    extracted_value=None,
                    status=target.status,
                    error_detail=error_detail,
                    checked_at=now,
                )
            )
            session.commit()
            return (
                TargetSnapshot.model_validate(target),
                AlertRecord.model_validate(alert_created) if alert_created else None,
            )

        return await self._execute(_op)
