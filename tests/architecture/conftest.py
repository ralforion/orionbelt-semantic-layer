"""Surface the architecture inventory in CI logs.

The inventory test (``test_inventory.py``) stashes its rendered report on
the pytest config. ``pytest_terminal_summary`` then prints it once at the
end of the session — terminal-summary output is shown regardless of
output capture, so the report is reliably visible in CI logs without
requiring ``-s`` or a particular verbosity flag. The summary only prints
when the inventory test actually ran, so unrelated narrow test selections
stay quiet.
"""

from __future__ import annotations

import pytest

INVENTORY_REPORT_KEY = pytest.StashKey[str]()


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    report = terminalreporter.config.stash.get(INVENTORY_REPORT_KEY, "")
    if report:
        terminalreporter.write_line("")
        terminalreporter.write_line(report)
