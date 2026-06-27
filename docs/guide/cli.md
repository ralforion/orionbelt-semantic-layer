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
| `obsl compile MODEL -q QUERY` | Compile a query to SQL | local or `--server` |
| `obsl execute MODEL -q QUERY` | Compile and run a query | local or `--server` |
| `obsl describe MODEL` | Structured overview of artefacts | local |
| `obsl diagram MODEL` | Mermaid ER diagram | local |
| `obsl graph MODEL` | OBSL-Core RDF graph (Turtle) | local |
| `obsl convert DIRECTION INPUT` | OSI ↔ OBML conversion | local or `--server` |
| `obsl dialects` | List supported SQL dialects | local or `--server` |

`MODEL` and `INPUT` accept a file path, or `-` to read from standard input.
Query documents (`-q`) may be JSON or YAML and use snake_case or camelCase
field names.

## Validate

Validation returns a non-zero exit code when the model is invalid, so it drops
straight into CI:

```bash
obsl validate model.yaml
# model is valid

obsl validate model.yaml -f json   # machine-readable result
```

```yaml
# .github/workflows/ci.yml
- run: obsl validate models/*.yaml
```

## Compile

```bash
obsl compile model.yaml -q query.json --dialect snowflake
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
obsl execute model.yaml -q query.json --server https://your-host --api-key "$OBSL_API_KEY"
obsl execute model.yaml -q query.json -f csv > results.csv
```

A default row limit (1000) is applied when the query has none.

## Describe, diagram, graph

```bash
obsl describe model.yaml            # tables of data objects, dimensions, measures, metrics
obsl diagram model.yaml > er.mmd    # Mermaid ER diagram
obsl graph model.yaml > model.ttl   # OBSL-Core RDF (Turtle)
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

In remote mode the local model file is uploaded with each call (via the
stateless `oneshot` / `validate` / `convert` endpoints), so no server-side
session has to be created.

```bash
export OBSL_SERVER=https://your-host
export OBSL_API_KEY=sk-...
obsl compile model.yaml -q query.json
```
