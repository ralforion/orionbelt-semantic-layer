"""Verify the PEP 249 exception hierarchy."""

from ob_driver_core.exceptions import (
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
)


def test_warning_is_exception() -> None:
    assert issubclass(Warning, Exception)


def test_error_is_exception() -> None:
    assert issubclass(Error, Exception)


def test_interface_error_is_error() -> None:
    assert issubclass(InterfaceError, Error)


def test_database_error_is_error() -> None:
    assert issubclass(DatabaseError, Error)


def test_data_error_is_database_error() -> None:
    assert issubclass(DataError, DatabaseError)


def test_operational_error_is_database_error() -> None:
    assert issubclass(OperationalError, DatabaseError)


def test_integrity_error_is_database_error() -> None:
    assert issubclass(IntegrityError, DatabaseError)


def test_internal_error_is_database_error() -> None:
    assert issubclass(InternalError, DatabaseError)


def test_programming_error_is_database_error() -> None:
    assert issubclass(ProgrammingError, DatabaseError)


def test_not_supported_error_is_database_error() -> None:
    assert issubclass(NotSupportedError, DatabaseError)


def test_can_raise_and_catch_by_parent() -> None:
    try:
        raise ProgrammingError("bad query")
    except DatabaseError as exc:
        assert str(exc) == "bad query"
    except Exception as exc:
        raise AssertionError("ProgrammingError should be catchable as DatabaseError") from exc
