"""A result's pages as one Arrow stream, for a consumer that reads it batch by batch.

A ``pyarrow.RecordBatchReader`` has one schema, and the pages of one result
need not agree on it: a page's types can depend on its values. Every page is
cast to the first page's schema, so a reader never changes type midway; a page
whose values that schema cannot hold fails the read and says so, rather than
streaming a value changed to fit.

Each driver package keeps its own copy, so a driver needs no newer
``ob-driver-core`` than the one it already requires.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pyarrow as pa

from ob_mysql.exceptions import DataError

#: Rows per page asked of a driver that pages by row count.
BATCH_ROWS = 65_536


def one_schema_reader(pages: Iterator[pa.Table], empty: pa.Schema) -> pa.RecordBatchReader:
    """*pages* as one reader under the first page's schema; *empty*'s without a page.

    The first page is read here, so an error the query raises on its first
    page surfaces at once; later pages are read as the reader is.
    """
    first = next(pages, None)
    if first is None:
        return pa.RecordBatchReader.from_batches(empty, [])
    schema = first.schema

    def batches() -> Iterator[pa.RecordBatch]:
        for page in itertools.chain([first], pages):
            if page.schema != schema:
                try:
                    page = page.cast(schema)
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
                    raise DataError(
                        f"A page of the result does not fit the first page's types: {exc}"
                    ) from exc
            yield from page.to_batches()

    return pa.RecordBatchReader.from_batches(schema, batches())
