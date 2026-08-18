"""Every vendor's seed literal survives a round trip through its own escaping.

The Databricks seed carried corrupted names for months: it doubled apostrophes
as the SQL standard says, and Spark SQL silently drops the character rather
than erroring. ``SELECT 'O''Brien'`` returns ``OBrien`` - six characters, no
complaint - so 500 rows of the commerce seed lost an apostrophe and only two
corpus queries ever noticed.

BigQuery needs the same backslash form and fails loudly, which is why it was
already handled. The lesson is the silent one: a vendor added without checking
its string escaping corrupts data rather than refusing it, and the only signal
is a golden comparison that happens to include an affected value.
"""

from __future__ import annotations

import pytest

from tests.integration.drift.vendor_exec._seed import _SPECS, VENDORS, _lit

# Measured per engine, not assumed. Postgres, MySQL, ClickHouse, DuckDB and
# Dremio take the doubled form; Snowflake accepts either; BigQuery and
# Databricks require the backslash.
BACKSLASH_VENDORS = {"bigquery", "databricks"}


@pytest.mark.parametrize("vendor", sorted(VENDORS))
def test_the_escape_style_matches_what_the_engine_accepts(vendor: str) -> None:
    assert _SPECS[vendor].backslash_escape is (vendor in BACKSLASH_VENDORS), (
        f"{vendor} would emit the wrong quote escaping for its engine"
    )


@pytest.mark.parametrize("vendor", sorted(VENDORS))
def test_an_apostrophe_is_escaped_rather_than_dropped(vendor: str) -> None:
    """The specific value that was corrupted, per vendor."""
    rendered = _lit("Alex O'Brien", backslash_escape=_SPECS[vendor].backslash_escape)
    expected = "'Alex O\\'Brien'" if _SPECS[vendor].backslash_escape else "'Alex O''Brien'"
    assert rendered == expected, f"{vendor}: {rendered}"


def test_a_backslash_is_escaped_before_the_quote() -> None:
    """Order matters: escape backslashes first or they consume the quote escape."""
    assert _lit("a\\'b", backslash_escape=True) == "'a\\\\\\'b'"
