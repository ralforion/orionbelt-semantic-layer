# Command-Line Interface (`obsl`)

`obsl` is the OrionBelt Semantic Layer command-line tool. It is **local-first**:
`validate`, `compile`, `describe`, `diagram`, `graph` and `convert` run
in-process by calling the same compiler, parser and converter the REST API
uses — so you can lint a model and preview the generated SQL with zero
infrastructure. This makes it a natural fit for CI pipelines and pre-commit
hooks.

Commands that benefit from a running engine (notably `execute`, which needs a
warehouse connection) can target a deployed server with `--server`.

## Installation

The CLI ships with the package and is registered as the `obsl` console script
alongside `orionbelt-api` and `orionbelt-ui`:

```bash
pip install orionbelt-semantic-layer
# or
uv pip install orionbelt-semantic-layer

obsl --help
obsl --version
```

## Commands

| Command | What it does | Runs |
| --- | --- | --- |
| `obsl validate MODEL` | Validate a model; exits `1` on error | local or `--server` |
| `obsl compile [MODEL] -q QUERY` | Compile a query to SQL | local or `--server` |
| `obsl execute [MODEL] -q QUERY` | Compile and run a query | local or `--server` |
| `obsl describe MODEL` | Structured overview of artefacts | local |
| `obsl diagram MODEL` | Mermaid ER diagram | local |
| `obsl graph MODEL` | OBSL-Core RDF graph (Turtle) | local |
| `obsl convert DIRECTION INPUT` | OSI ↔ OBML conversion | local or `--server` |
| `obsl dialects` | List supported SQL dialects | local or `--server` |

`MODEL` and `INPUT` accept a file path, or `-` to read from standard input.

For `compile` and `execute` you supply the query one of two ways (exactly one):

- `-q / --query` — a query **document** (JSON or YAML; snake_case or camelCase fields)
- `--sql` — an **OrionBelt Semantic QL (OBSQL)** string, BI-style SQL against the
  model's virtual table: `SELECT <dim/measure labels> FROM <model> [WHERE ...]
  [ORDER BY ...] [LIMIT n]`

## Options reference

`obsl <command> --help` is always authoritative. Every option:

| Command | Options |
| --- | --- |
| _common (where remote-capable)_ | `-f, --format {table,json,csv,tsv}` · `-s, --server URL` (env `OBSL_SERVER`) · `--api-key KEY` (env `OBSL_API_KEY`) |
| `validate` | `-f/--format` · `-s/--server` · `--api-key` |
| `compile` | `-q/--query PATH` · `--sql TEXT` · `-d/--dialect NAME` · `--explain` · `--pretty/--no-pretty` (default pretty) · `-f/--format` · `-s/--server` · `--api-key` |
| `execute` | `-q/--query PATH` · `--sql TEXT` · `-d/--dialect NAME` · `--limit N` (default 1000; see note) · `-f/--format` · `-s/--server` · `--api-key` |
| `describe` | `-f/--format` |
| `diagram` | `--columns/--no-columns` (default columns) · `--theme NAME` (Mermaid theme, default `default`) |
| `graph` | _(none)_ |
| `convert` | `DIRECTION` (`osi-to-obml`\|`obml-to-osi`) · `INPUT` · `--ontology` (obml-to-osi only) · `--name NAME` (OSI model name, obml-to-osi) · `-s/--server` · `--api-key` |
| `dialects` | `-f/--format` · `-s/--server` · `--api-key` |

Global: `-V/--version`, `--install-completion`, `--show-completion`.

`--limit` applies to `-q` queries and **local** `--sql` (when the query has no
limit). It cannot apply to **remote** `--sql` (the server's OBSQL endpoint takes
no limit) — put `LIMIT n` in the SQL there; the CLI warns if you pass both.

## Validate

Validation returns a non-zero exit code when the model is invalid, so it drops
straight into CI:

```bash
obsl validate model.yaml
# model is valid

obsl validate model.yaml -f json   # machine-readable result
```

```yaml
# .github/workflows/ci.yml — validate every model, fail on the first invalid one
- run: |
    for m in models/*.yaml; do obsl validate "$m" || exit 1; done
```

## Compile

```bash
obsl compile model.yaml -q query.json --dialect snowflake
# or with an OBSQL string instead of a query document:
obsl compile model.yaml --sql 'SELECT "Customer Country", "Revenue" FROM model LIMIT 5'
```

Add `--explain` to print the planner's decisions (chosen planner, base object,
join path, CFL legs) to stderr while the SQL stays on stdout, so piping is
unaffected:

```bash
obsl compile model.yaml -q query.json -d postgres --explain > out.sql
```

A minimal query document:

```json
{
  "select": { "dimensions": ["Customer Country"], "measures": ["Total Revenue"] },
  "limit": 10
}
```

## Execute

`execute` compiles and runs the query against the configured warehouse. Running
locally requires database drivers and credentials to be configured (see
[Configuration](../reference/configuration.md)); otherwise point it at a
deployed engine:

```bash
obsl execute model.yaml -q query.json -f csv > results.csv          # local
obsl execute model.yaml -q query.json --limit 50                    # cap rows when the query has none
obsl execute -q query.json --server https://your-host --api-key "$OBSL_API_KEY"  # remote
```

`--limit` (default 1000) applies when the query carries no limit of its own. It
covers `-q` queries and local `--sql`; for remote `--sql`, put `LIMIT n` in the
SQL (the CLI warns if you pass `--limit` there).

## Describe, diagram, graph

```bash
obsl describe model.yaml                       # tables of data objects, dimensions, measures, metrics
obsl diagram model.yaml > er.mmd               # Mermaid ER diagram
obsl diagram model.yaml --no-columns --theme dark   # compact entities, dark theme
obsl graph model.yaml > model.ttl              # OBSL-Core RDF (Turtle)
```

## Convert (OSI ↔ OBML)

OrionBelt interoperates with the
[Open Semantic Interchange (OSI)](https://open-semantic-interchange.org)
format:

```bash
obsl convert obml-to-osi model.yaml > model.osi.yaml
obsl convert obml-to-osi model.yaml --ontology          # also emit the OSI ontology
obsl convert osi-to-obml model.osi.yaml > model.yaml
```

Conversion requires the optional converter: `pip install 'orionbelt-semantic-layer[osi]'`.

## Output formats and streams

The `--format` / `-f` flag controls tabular output (`table`, `json`, `csv`,
`tsv`). Data is written to **stdout**; informational notes, warnings and errors
go to **stderr** — so `obsl ... -f json | jq` and redirects work cleanly.

## Remote mode

| Flag | Env var | Purpose |
| --- | --- | --- |
| `--server URL` | `OBSL_SERVER` | Target a deployed OrionBelt REST API |
| `--api-key KEY` | `OBSL_API_KEY` | API key for that server |

`compile` and `execute` in remote mode run the query against the **server's
curated model** (via the `/v1/query/sql` and `/v1/query/execute` shortcuts that
auto-resolve the deployed model) — no model is uploaded, so `MODEL` is omitted
and governed single-model deployments (where ad-hoc model upload is disabled)
are respected. `validate` and `convert` operate on the model you pass.

```bash
export OBSL_SERVER=https://your-host
export OBSL_API_KEY=sk-...
obsl compile -q query.json                                  # query document
obsl execute --sql 'SELECT "Region", "Sales" FROM model'    # OBSQL string
obsl validate model.yaml                                     # validates the model you pass
```
