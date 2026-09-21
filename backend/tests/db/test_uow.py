"""Unit-of-work boundary behaviour.

The outcome matrix only works if operational failures reach the caller as
``TransientStorageError``. Anything else would be acknowledged as if it had
been stored.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from powerguard.db.uow import SqlUnitOfWork, SqlUnitOfWorkFactory
from powerguard.domain.errors import TransientStorageError
from tests import builders
from tests.builders import BOOT_ID, DEVICE_ID, FIRMWARE


class BrokenSession:
    """A session whose operations fail the way a dead connection would."""

    def __init__(
        self,
        *,
        fail_commit: bool = True,
        fail_rollback: bool = True,
        fail_close: bool = False,
    ) -> None:
        self.closed = False
        self.close_attempts = 0
        self._fail_commit = fail_commit
        self._fail_rollback = fail_rollback
        self._fail_close = fail_close

    def _boom(self, detail: str = "database is locked") -> OperationalError:
        return OperationalError("stmt", {}, Exception(detail))

    def commit(self) -> None:
        if self._fail_commit:
            raise self._boom()

    def rollback(self) -> None:
        if self._fail_rollback:
            raise self._boom()

    def close(self) -> None:
        self.close_attempts += 1
        if self._fail_close:
            raise self._boom("cannot close a broken connection")
        self.closed = True


def broken_factory(session: BrokenSession) -> sessionmaker[Session]:
    return lambda: session  # type: ignore[return-value]


def test_leaving_without_committing_rolls_back(
    uow_factory: SqlUnitOfWorkFactory, clock: object
) -> None:
    now = dt.datetime.now(dt.UTC)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, now)
        # No commit: the write must not survive the block.

    with uow_factory() as uow:
        assert uow.devices.get(DEVICE_ID) is None


def test_an_exception_inside_the_block_rolls_back_and_propagates(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    now = dt.datetime.now(dt.UTC)
    with pytest.raises(RuntimeError, match="caller failed"), uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, now)
        uow.commit()
        raise RuntimeError("caller failed")

    # The commit already happened, so that write is durable; the point is that
    # the failure is not swallowed.
    with uow_factory() as uow:
        assert uow.devices.get(DEVICE_ID) is not None


def test_explicit_rollback_discards_uncommitted_work(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    now = dt.datetime.now(dt.UTC)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, now)
        uow.rollback()
        uow.commit()

    with uow_factory() as uow:
        assert uow.devices.get(DEVICE_ID) is None


def test_a_failed_commit_is_reported_as_transient() -> None:
    session = BrokenSession(fail_rollback=False)
    uow = SqlUnitOfWork(broken_factory(session))
    with uow, pytest.raises(TransientStorageError, match="locked"):
        uow.commit()
    assert session.closed is True


def test_a_failed_rollback_on_exit_is_reported_as_transient() -> None:
    session = BrokenSession()
    uow = SqlUnitOfWork(broken_factory(session))
    with pytest.raises(TransientStorageError, match="locked"), uow:
        pass
    assert session.closed is True, "the session is released even when rollback fails"


def test_a_failed_rollback_never_masks_the_original_error() -> None:
    session = BrokenSession()
    uow = SqlUnitOfWork(broken_factory(session))
    with pytest.raises(ValueError, match="the real problem"), uow:
        raise ValueError("the real problem")
    assert session.closed is True


def test_explicit_rollback_failure_is_transient() -> None:
    session = BrokenSession()
    uow = SqlUnitOfWork(broken_factory(session))
    uow.__enter__()
    try:
        with pytest.raises(TransientStorageError):
            uow.rollback()
    finally:
        uow._session = None


def test_using_an_inactive_unit_of_work_is_a_programming_error(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    uow = uow_factory()
    with pytest.raises(RuntimeError, match="not active"):
        uow.commit()


def test_a_second_write_after_commit_needs_its_own_transaction(
    uow_factory: SqlUnitOfWorkFactory,
) -> None:
    now = dt.datetime.now(dt.UTC)
    with uow_factory() as uow:
        uow.devices.upsert_from_telemetry(DEVICE_ID, FIRMWARE, BOOT_ID, now)
        stored = uow.telemetry.insert_or_resolve_duplicate(
            builders.telemetry(received_at=now)
        )
        uow.commit()
    assert stored.id is not None  # type: ignore[union-attr]

    with uow_factory() as uow:
        count = uow._session.execute(
            text("SELECT COUNT(*) FROM telemetry")
        ).scalar_one()
    assert count == 1


# --- rollback and close failures never escape raw -------------------------


def test_a_failed_close_is_reported_as_transient() -> None:
    """`close()` is as much a storage operation as `rollback()`.

    Letting a raw SQLAlchemyError out here would bypass the outcome matrix
    entirely: ingestion only withholds the acknowledgement for
    TransientStorageError, so the message would be acknowledged although the
    transaction never landed cleanly.
    """
    session = BrokenSession(fail_rollback=False, fail_close=True)
    uow = SqlUnitOfWork(broken_factory(session))

    with pytest.raises(TransientStorageError, match="cannot close"), uow:
        pass

    assert session.close_attempts == 1


def test_a_failed_rollback_and_a_failed_close_report_the_rollback() -> None:
    """The first failure explains what went wrong; both are still attempted."""
    session = BrokenSession(fail_close=True)
    uow = SqlUnitOfWork(broken_factory(session))

    with pytest.raises(TransientStorageError, match="locked"), uow:
        pass

    assert session.close_attempts == 1, "the session is released even so"


def test_a_failed_close_never_masks_the_original_error() -> None:
    session = BrokenSession(fail_rollback=False, fail_close=True)
    uow = SqlUnitOfWork(broken_factory(session))

    with pytest.raises(ValueError, match="the real problem"), uow:
        raise ValueError("the real problem")

    assert session.close_attempts == 1


def test_a_failed_close_after_a_successful_commit_is_still_transient() -> None:
    session = BrokenSession(fail_commit=False, fail_rollback=False, fail_close=True)
    uow = SqlUnitOfWork(broken_factory(session))

    with pytest.raises(TransientStorageError), uow:
        uow.commit()

    assert session.close_attempts == 1


def test_a_failed_rollback_inside_commit_does_not_replace_the_commit_error() -> None:
    """Both fail; the reported cause is the commit, which is the useful one."""
    session = BrokenSession(fail_commit=True, fail_rollback=True)
    uow = SqlUnitOfWork(broken_factory(session))
    uow.__enter__()

    with pytest.raises(TransientStorageError) as excinfo:
        uow.commit()
    assert isinstance(excinfo.value.__cause__, OperationalError)
    assert "locked" in str(excinfo.value)

    # Leaving the block fails too, and still stays inside the domain error type.
    with pytest.raises(TransientStorageError):
        uow.__exit__(None, None, None)
    assert session.close_attempts == 1


def test_no_raw_sqlalchemy_error_escapes_the_unit_of_work() -> None:
    """The boundary guarantee, stated as one assertion.

    Every failing combination of commit, rollback and close is exercised; none
    may surface as a bare SQLAlchemyError.
    """
    for fail_commit in (True, False):
        for fail_rollback in (True, False):
            for fail_close in (True, False):
                session = BrokenSession(
                    fail_commit=fail_commit,
                    fail_rollback=fail_rollback,
                    fail_close=fail_close,
                )
                uow = SqlUnitOfWork(broken_factory(session))
                try:
                    with uow, contextlib.suppress(TransientStorageError):
                        uow.commit()
                except TransientStorageError:
                    pass
                except SQLAlchemyError as exc:  # pragma: no cover - the defect
                    raise AssertionError(
                        f"raw {type(exc).__name__} escaped for commit={fail_commit} "
                        f"rollback={fail_rollback} close={fail_close}"
                    ) from exc
                assert session.close_attempts == 1
