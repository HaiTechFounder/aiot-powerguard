"""Session-scoped unit of work.

One unit of work owns exactly one SQLAlchemy session and is used inside a
single thread. Leaving the context without an explicit commit rolls back, so a
half-applied transaction can never be mistaken for a successful write.
"""

from __future__ import annotations

import contextlib
from types import TracebackType

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from powerguard.db.repositories import (
    SqlAnomalyRepository,
    SqlDeviceRepository,
    SqlTelemetryRepository,
)
from powerguard.domain.errors import TransientStorageError
from powerguard.domain.ports import AnomalyRepository, DeviceRepository, TelemetryRepository


class SqlUnitOfWork:
    # Declared with the port types so the class structurally satisfies the
    # UnitOfWork protocol, which treats these as mutable (invariant) attributes.
    devices: DeviceRepository
    telemetry: TelemetryRepository
    anomalies: AnomalyRepository

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._committed = False

    def __enter__(self) -> SqlUnitOfWork:
        self._session = self._session_factory()
        self._committed = False
        self.devices = SqlDeviceRepository(self._session)
        self.telemetry = SqlTelemetryRepository(self._session)
        self.anomalies = SqlAnomalyRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._require_session()
        # Both steps are operational failures when they fail: reported as
        # transient so the caller withholds the acknowledgement instead of a
        # raw SQLAlchemy error leaking through the outcome matrix. Neither may
        # mask an exception that is already propagating, and the session is
        # released whatever happens.
        failure: SQLAlchemyError | None = None
        try:
            if exc_type is not None or not self._committed:
                try:
                    session.rollback()
                except SQLAlchemyError as rollback_error:
                    failure = rollback_error
        finally:
            try:
                session.close()
            except SQLAlchemyError as close_error:
                failure = failure or close_error
            finally:
                self._session = None

        if failure is not None and exc_type is None:
            raise TransientStorageError(str(failure)) from failure

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session

    def commit(self) -> None:
        session = self._require_session()
        try:
            session.commit()
        except SQLAlchemyError as exc:
            # A rollback that also fails must not replace the commit failure,
            # which is the one that explains what went wrong.
            with contextlib.suppress(SQLAlchemyError):
                session.rollback()
            raise TransientStorageError(str(exc)) from exc
        self._committed = True

    def rollback(self) -> None:
        session = self._require_session()
        try:
            session.rollback()
        except SQLAlchemyError as exc:
            raise TransientStorageError(str(exc)) from exc
        self._committed = False


class SqlUnitOfWorkFactory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._session_factory)
