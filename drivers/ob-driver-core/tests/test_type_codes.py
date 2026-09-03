"""Verify PEP 249 type objects."""

from ob_driver_core.type_codes import BINARY, DATETIME, NUMBER, ROWID, STRING, DBAPITypeObject


def test_string_matches() -> None:
    assert STRING == "STRING"
    assert STRING == "VARCHAR"
    assert STRING == "TEXT"
    assert STRING != "INTEGER"


def test_number_matches() -> None:
    assert NUMBER == "INT"
    assert NUMBER == "INTEGER"
    assert NUMBER == "BIGINT"
    assert NUMBER == "FLOAT"
    assert NUMBER == "DECIMAL"
    assert NUMBER != "VARCHAR"


def test_datetime_matches() -> None:
    assert DATETIME == "TIMESTAMP"
    assert DATETIME == "DATE"
    assert DATETIME == "TIMESTAMP_NTZ"
    assert DATETIME != "INT"


def test_binary_matches() -> None:
    assert BINARY == "BLOB"
    assert BINARY == "BYTEA"
    assert BINARY != "TEXT"


def test_rowid_matches() -> None:
    assert ROWID == "ROWID"
    assert ROWID != "INT"


def test_equality_between_type_objects() -> None:
    other = DBAPITypeObject("STRING", "VARCHAR", "TEXT", "CHAR", "NVARCHAR", "NCHAR")
    assert other == STRING


def test_repr() -> None:
    r = repr(ROWID)
    assert "ROWID" in r
    assert "DBAPITypeObject" in r


def test_hash_stable() -> None:
    a = DBAPITypeObject("A", "B")
    b = DBAPITypeObject("B", "A")
    assert hash(a) == hash(b)
    assert a == b
