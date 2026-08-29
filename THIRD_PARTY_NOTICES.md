# Third-party notices

OrionBelt Semantic Layer is distributed under the [Business Source License 1.1](LICENSE)
(SPDX: `BUSL-1.1`), converting to Apache License 2.0 on 2030-03-16.

This document covers third-party works that OrionBelt **redistributes**, with
their sources, licences, and where their licence text lives. Attribution
obligations attach to redistribution, so the two cases are listed separately:
what ships inside the wheel, and what ships inside the container images.

## Bundled in the Python wheel

One third-party file is copied into the package and published on PyPI as part
of `orionbelt-semantic-layer`.

| Shipped as | Upstream | Version | Licence | Text |
|---|---|---|---|---|
| `orionbelt/ui/static/vis-network.min.js` | [vis-network](https://visjs.github.io/vis-network/) | 9.1.2 | Apache-2.0 OR MIT (redistributed under MIT) | `orionbelt/ui/static/vis-network.LICENSE.txt` |

vis-network is dual licensed and may be distributed under either licence;
OrionBelt redistributes it under the MIT License. The minified file carries a
header naming both licences but **not** their text, which MIT requires to
accompany redistribution — hence the adjacent `.LICENSE.txt`.

It powers the Ontology Graph tab in the Gradio UI. Nothing else in the wheel is
third-party: the JSON Schemas under `orionbelt/schema/` and the ontology under
`ontology/` are OrionBelt's own work.

## Bundled in the container images

`ralforion/orionbelt-api`, `ralforion/orionbelt-ui` and
`ralforion/orionbelt-flight` contain the full installed dependency tree, so
publishing them is redistribution of those packages too. Each keeps its own
licence text in its `dist-info/licenses/` directory inside the image; the table
below records the direct runtime dependencies and their terms.

| Package | Licence |
|---|---|
| fastapi | MIT |
| httpx | BSD-3-Clause |
| jsonschema | MIT |
| networkx | BSD-3-Clause |
| opentelemetry-api | Apache-2.0 |
| pydantic | MIT |
| pydantic-settings | MIT |
| pytz | MIT |
| pyyaml | MIT |
| rdflib | BSD-3-Clause |
| ruamel.yaml | MIT |
| sqlglot | MIT |
| structlog | MIT OR Apache-2.0 |
| typer | MIT |
| tzdata | Apache-2.0 |
| uvicorn | BSD-3-Clause |

All are permissive (MIT, BSD-3-Clause, Apache-2.0). None is copyleft, so none
imposes a licensing obligation on OrionBelt's own source. Optional extras
(`ui`, `flight`, `drivers`, `docs`) and the vendor database drivers pull in
further packages under the same kinds of terms; they are installed by pip from
PyPI rather than copied into this repository.

Transitive dependencies are not enumerated here. They are resolved and pinned
in [`uv.lock`](uv.lock), which records the exact version of every package in a
build, and each carries its own licence in its distribution metadata.

## Not redistributed

Installing `orionbelt-semantic-layer` from PyPI pulls its dependencies from
PyPI directly — OrionBelt does not vendor or re-publish them, so that path
redistributes nothing but OrionBelt's own code and the one bundled file above.

## Where these terms came from

- vis-network: the `@license` header of the bundled `vis-network.min.js`
  (version 9.1.2, dated 2022-03-28) and the upstream project page.
- Python packages: the `License-Expression`, `License`, and `License ::`
  classifier fields of each installed distribution's metadata, read from the
  resolved environment rather than transcribed by hand.

## OrionBelt's own components

The workspace publishes several packages under their own terms:

| Package | Licence |
|---|---|
| `orionbelt-semantic-layer` | BUSL-1.1 |
| `ob-driver-core`, `ob-bigquery`, `ob-clickhouse`, `ob-databricks`, `ob-dremio`, `ob-duckdb`, `ob-flight-extension`, `ob-mysql`, `ob-postgres`, `ob-snowflake` | BUSL-1.1 |
| `osi-orionbelt` | Apache-2.0 |

`osi-orionbelt` is deliberately Apache-2.0: it implements the Apache Ossie
(formerly OSI) interchange format and is meant to be usable by anyone working
with that specification, independently of OrionBelt.
