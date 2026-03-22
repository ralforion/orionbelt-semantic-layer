"""Unit tests for OBML YAML detection — no external services required."""

import pytest

from ob_mysql.compiler import is_obml, parse_obml


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\n", True),
        ("select:\n  measures:\n    - Revenue\n", True),
        ("select:\n  dimensions:\n    - Country\n", True),
        ("SELECT:\n  dimensions:\n    - X\n", True),
        ("   select:\n  dimensions:\n    - X\n", True),
        ("SELECT * FROM orders", False),
        ("SELECT id\nFROM customers WHERE id = 1", False),
        ("", False),
        ("model:\n  name: test\n", False),
        ("select:\n  filters:\n    - x = 1\n", False),
        ("select:\n  - [unclosed", False),
        ("select:\n  - dimension1\n", False),
    ],
)
def test_obml_detection(query: str, expected: bool) -> None:
    assert is_obml(query) == expected


def test_parse_obml_returns_dict() -> None:
    q = "select:\n  dimensions:\n    - Region\n  measures:\n    - Revenue\nlimit: 100\n"
    result = parse_obml(q)
    assert result["select"]["dimensions"] == ["Region"]
    assert result["select"]["measures"] == ["Revenue"]
    assert result["limit"] == 100
