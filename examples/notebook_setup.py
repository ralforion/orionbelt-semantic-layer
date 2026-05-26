"""Quickstart notebook setup — hides infrastructure details from business users."""

from __future__ import annotations

import atexit
import gc
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Step 0: Create TPC-H database
# ---------------------------------------------------------------------------

def create_tpch_database(db_path: str = "tpch.duckdb", scale: float = 0.01) -> None:
    """Create a DuckDB TPC-H database (removes stale file first)."""
    _pip_map = {
        "duckdb": "duckdb",
        "yaml": "pyyaml",
        "pygments": "pygments",
        "sqlparse": "sqlparse",
    }
    for pkg, pip_name in _pip_map.items():
        try:
            __import__(pkg)
        except ImportError:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q",
                 "--disable-pip-version-check", pip_name],
            )

    import duckdb

    if os.path.exists(db_path):
        os.remove(db_path)

    con = duckdb.connect(db_path)
    con.execute("INSTALL tpch")
    con.execute("LOAD tpch")
    con.execute(f"CALL dbgen(sf={scale})")

    tables = con.execute(
        "SELECT table_name, estimated_size FROM duckdb_tables() ORDER BY table_name"
    ).fetchall()
    for name, size in tables:
        print(f"  {name:12s}  {size:>8,} rows")
    con.close()
    del con
    gc.collect()
    print(f"\nDatabase ready: {db_path}")


# ---------------------------------------------------------------------------
# Step 1: Start API + helpers
# ---------------------------------------------------------------------------

_api_base: str = ""
_api_process: subprocess.Popen | None = None  # type: ignore[type-arg]


def start_api(
    port: int = 8099,
    timeout: int = 120,
) -> tuple[str, str]:
    """Start the OrionBelt API and return ``(session_id, model_id)``."""
    global _api_base, _api_process  # noqa: PLW0603

    repo_root = os.path.abspath("..")
    _api_base = f"http://localhost:{port}"

    # Kill previous process from this notebook (safe to re-run)
    if _api_process is not None and _api_process.poll() is None:
        _api_process.terminate()
        _api_process.wait()

    # Kill leftover process on the port (survives kernel restart)
    try:
        pids = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True,
        ).strip()
        for pid in pids.splitlines():
            os.kill(int(pid), signal.SIGTERM)
        time.sleep(0.5)
    except (subprocess.CalledProcessError, ValueError):
        pass

    env = {
        **os.environ,
        "QUERY_EXECUTE": "true",
        "DB_VENDOR": "duckdb",
        "DUCKDB_DATABASE": os.path.join(repo_root, "examples", "tpch.duckdb"),
        # ``MODEL_FILE`` was removed in v2.7.0 — use ``MODEL_FILES``
        # (comma-separated, single-entry list is the direct equivalent).
        "MODEL_FILES": os.path.join(repo_root, "examples", "tpch.obml.yml"),
        "API_SERVER_PORT": str(port),
    }

    log = open("api.log", "w")  # noqa: SIM115
    _api_process = subprocess.Popen(
        ["uv", "run", "--extra", "flight", "orionbelt-api"],
        cwd=repo_root, env=env, stdout=log, stderr=log,
    )
    atexit.register(_api_process.terminate)

    for i in range(timeout):
        if _api_process.poll() is not None:
            log.flush()
            raise RuntimeError(
                f"API exited with code {_api_process.returncode}\n"
                + open("api.log").read()[-2000:]
            )
        try:
            urllib.request.urlopen(f"{_api_base}/health", timeout=2)
            print(f"API ready on port {port} ({i + 1}s)")
            break
        except Exception:
            time.sleep(1)
    else:
        _api_process.terminate()
        log.flush()
        raise RuntimeError(
            "API did not start in time\n" + open("api.log").read()[-2000:]
        )

    # MODEL_FILES triggers admin-curated mode: each YAML loads into its
    # own named protected session (addressed by filename stem or the OBML
    # ``name:`` field) — no need to create a session manually. Shortcut
    # endpoints (``/v1/schema``, ``/v1/query/sql``, ``/v1/query/execute``,
    # …) consult ``list_protected_session_ids()`` and resolve to the
    # single loaded model automatically (v2.7.0+).
    schema = api("GET", "/v1/schema")
    dims = len(schema.get("dimensions", []))
    measures = len(schema.get("measures", []))
    metrics = len(schema.get("metrics", []))
    print(f"Model ready: {dims} dimensions, {measures} measures, {metrics} metrics")
    return "", ""


def api(method: str, path: str, body: dict | str | None = None) -> dict | list | str:
    """Minimal HTTP helper — accepts a dict or YAML string as body."""
    import yaml as _yaml

    url = f"{_api_base}{path}"
    if isinstance(body, str):
        body = _yaml.safe_load(body)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req) as resp:
            text = resp.read().decode()
    except urllib.request.HTTPError as e:
        text = e.read().decode()
        print(f"HTTP {e.code}: {text}")
        raise
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# ---------------------------------------------------------------------------
# Display helpers — syntax-highlighted SQL/YAML + styled result tables
# ---------------------------------------------------------------------------

class _pg:
    """Lazy-initialized pygments + sqlparse state."""

    highlight = None
    formatter = None
    sql_lexer = None
    yaml_lexer = None
    format_sql = None

    @classmethod
    def init(cls) -> None:
        if cls.highlight is not None:
            return

        import sqlparse
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name
        from pygments.style import Style
        from pygments.token import (
            Comment,
            Error,
            Keyword,
            Literal,
            Name,
            Number,
            Operator,
            Punctuation,
            String,
            Token,
        )

        # VS Code Dark+ faithful color mapping
        # SQL: keywords=blue, quoted identifiers=white, functions=blue,
        #      numbers=green, operators/punctuation=white
        # YAML: keys(Name.Tag)=blue, values(Literal.Scalar)=orange,
        #       list items(Name.Variable)=orange, numbers=green
        class VSCodeDark(Style):  # type: ignore[misc]
            name = "vscode-dark"
            background_color = "#1e1e1e"
            styles = {
                Token: "#d4d4d4",
                # comments
                Comment: "italic #6a9955",
                Comment.Single: "italic #6a9955",
                Comment.Multiline: "italic #6a9955",
                # SQL keywords — blue, bold
                Keyword: "bold #569cd6",
                Keyword.DML: "bold #569cd6",
                Keyword.DDL: "bold #569cd6",
                Keyword.Type: "#4ec9b0",
                # identifiers — white (VS Code doesn't color SQL identifiers)
                Name: "#d4d4d4",
                Name.Function: "#d4d4d4",
                Name.Builtin: "#d4d4d4",
                # YAML keys — blue
                Name.Tag: "#569cd6",
                # YAML values in flow sequences — orange
                Name.Variable: "#ce9178",
                Name.Attribute: "#9cdcfe",
                Name.Class: "#4ec9b0",
                Name.Decorator: "#dcdcaa",
                # strings — orange for regular, white for SQL quoted identifiers
                String: "#ce9178",
                String.Single: "#ce9178",
                String.Double: "#ce9178",
                String.Symbol: "#ce9178",  # "Column" quoted identifiers → orange
                # numbers — light green
                Number: "#b5cea8",
                Number.Integer: "#b5cea8",
                Number.Float: "#b5cea8",
                # YAML plain scalars — orange
                Literal: "#ce9178",
                Literal.String: "#ce9178",
                Literal.Scalar: "#ce9178",
                # operators and punctuation — white
                Operator: "#d4d4d4",
                Operator.Word: "bold #569cd6",
                Punctuation: "#d4d4d4",
                Punctuation.Indicator: "#d4d4d4",
                Error: "#f44747",
            }

        cls.highlight = highlight
        cls.sql_lexer = get_lexer_by_name("sql")
        cls.yaml_lexer = get_lexer_by_name("yaml")
        cls.formatter = HtmlFormatter(style=VSCodeDark, noclasses=True, nowrap=False)

        def fmt(sql: str) -> str:
            formatted = sqlparse.format(
                sql.strip(),
                reindent=True,
                keyword_case="upper",
                indent_columns=True,
                indent_width=2,
            )
            # Indent JOIN lines under FROM
            lines = formatted.splitlines()
            out: list[str] = []
            for line in lines:
                stripped = line.lstrip()
                if stripped.upper().startswith(("LEFT JOIN", "RIGHT JOIN",
                    "INNER JOIN", "CROSS JOIN", "FULL JOIN", "JOIN ")):
                    out.append("  " + line)
                else:
                    out.append(line)
            return "\n".join(out)

        cls.format_sql = staticmethod(fmt)

# VS Code Dark+ inspired palette
_HEADER_BG = "#252526"
_HEADER_FG = "#4ec9b0"
_ROW_EVEN = "#1e1e1e"
_ROW_ODD = "#252526"
_CELL_FG = "#d4d4d4"
_BORDER = "#3c3c3c"
_LABEL_FG = "#569cd6"


def _section_label(text: str) -> str:
    return (
        f'<div style="font-family:\'Cascadia Code\',\'Fira Code\',Consolas,monospace;'
        f" font-size:11px; font-weight:600; color:{_LABEL_FG};"
        f' margin:14px 0 4px 0; text-transform:uppercase; letter-spacing:1.5px;">'
        f"{text}</div>"
    )


def show_sql(sql: str) -> str:
    """Return HTML with syntax-highlighted, reformatted SQL."""
    _pg.init()
    formatted = _pg.format_sql(sql)
    return _section_label("SQL") + _pg.highlight(formatted, _pg.sql_lexer, _pg.formatter)


def show_yaml(yaml_str: str) -> str:
    """Return HTML with syntax-highlighted YAML (arrays as block lists)."""
    import yaml as _yaml

    class _IndentedDumper(_yaml.Dumper):
        """Indent list items under their parent key."""

        def increase_indent(self, flow: bool = False, _indentless: bool = False) -> None:
            return super().increase_indent(flow, False)

    _pg.init()
    data = _yaml.safe_load(yaml_str)
    block = _yaml.dump(
        data, Dumper=_IndentedDumper,
        default_flow_style=False, sort_keys=False, allow_unicode=True,
    )
    return (
        _section_label("OBML Query")
        + _pg.highlight(block.strip(), _pg.yaml_lexer, _pg.formatter)
    )


def show_table(columns: list[dict], rows: list[list]) -> str:
    """Return an HTML table with styled header, alternating rows, right-aligned numbers."""
    col_names = [c["name"] for c in columns]

    # Detect numeric columns from first data row
    numeric: list[bool] = []
    for i in range(len(col_names)):
        val = rows[0][i] if rows else None
        numeric.append(isinstance(val, (int, float)))

    header_cells = "".join(
        f'<th style="padding:8px 14px; text-align:{"right" if numeric[i] else "left"};'
        f" border-bottom:2px solid {_LABEL_FG}; color:{_HEADER_FG};"
        f' font-size:13px;">{name}</th>'
        for i, name in enumerate(col_names)
    )

    body_rows: list[str] = []
    for idx, row in enumerate(rows):
        bg = _ROW_EVEN if idx % 2 == 0 else _ROW_ODD
        cells: list[str] = []
        for i, val in enumerate(row):
            align = "right" if numeric[i] else "left"
            if isinstance(val, float):
                if abs(val) < 1 and val != 0:
                    formatted = f"{val:.2%}"
                else:
                    formatted = f"{val:,.2f}"
            elif isinstance(val, int):
                formatted = f"{val:,}"
            else:
                formatted = str(val)
            cells.append(
                f'<td style="padding:6px 14px; text-align:{align}; color:{_CELL_FG};'
                f' border-bottom:1px solid {_BORDER}; font-size:13px;">{formatted}</td>'
            )
        body_rows.append(f'<tr style="background:{bg};">{"".join(cells)}</tr>')

    return (
        _section_label(f"Result &mdash; {len(rows)} row{'s' if len(rows) != 1 else ''}")
        + f'<table style="border-collapse:collapse; background:{_ROW_EVEN};'
        f" border-radius:6px; overflow:hidden; margin-bottom:12px;"
        f" font-family:'Cascadia Code','Fira Code',Consolas,monospace;\">"
        f'<thead><tr style="background:{_HEADER_BG};">{header_cells}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def show_json(data: object) -> str:
    """Return HTML with syntax-highlighted JSON."""
    import json as _json

    from pygments.lexers import get_lexer_by_name

    _pg.init()
    lexer = get_lexer_by_name("json")
    text = _json.dumps(data, indent=2)
    return _section_label("JSON") + _pg.highlight(text, lexer, _pg.formatter)


def show_mermaid(mermaid_src: str, height: int = 800) -> None:
    """Render a Mermaid diagram via mermaid.js (no extension required)."""
    import hashlib

    from IPython.display import HTML, display

    uid = "m" + hashlib.md5(mermaid_src.encode()).hexdigest()[:8]
    html = f"""
<div id="{uid}" style="background:#1e1e1e; border-radius:6px;
     padding:16px; min-height:{height}px; overflow:auto;"></div>
<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
mermaid.initialize({{ startOnLoad: false, theme: 'dark' }});
const {{ svg }} = await mermaid.render('{uid}_svg', `{mermaid_src}`);
document.getElementById('{uid}').innerHTML = svg;
</script>
"""
    display(HTML(html))


def show_result(
    result: dict,
    query: str | None = None,
) -> None:
    """Display a complete query result: YAML query + SQL + table."""
    from IPython.display import HTML, display

    parts: list[str] = []
    if query is not None:
        parts.append(show_yaml(query))
    parts.append(show_sql(result["sql"]))
    if result.get("rows"):
        parts.append(show_table(result["columns"], result["rows"]))
    display(HTML("".join(parts)))
