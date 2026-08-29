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

Publishing an image redistributes everything installed inside it, so the images
carry more than the wheel does. Each is built from a different optional-
dependency group, so they differ from one another too; the tables list what
each group adds **on top of** the runtime dependencies above.

Every package keeps its own licence text in its `dist-info/licenses/`
directory inside the image.

### `ralforion/orionbelt-semantic-layer-api`

Built from `Dockerfile` with `OB_EXTRA` defaulting to `flight-duckdb-only`.

| Package | Licence |
|---|---|
| pyarrow | Apache-2.0 |
| ob-flight-extension, ob-driver-core, ob-duckdb, osi-orionbelt | OrionBelt's own — see below |

### `ralforion/orionbelt-semantic-layer-flight`

Built from `Dockerfile.flight` with the `flight` extra, which adds every vendor
driver.

| Package | Licence |
|---|---|
| pyarrow | Apache-2.0 |
| ob-flight-extension, ob-driver-core, osi-orionbelt, and the nine `ob-<vendor>` drivers | OrionBelt's own — see below |

The vendor drivers each depend on that vendor's own client SDK
(`snowflake-connector-python`, `databricks-sql-connector`,
`google-cloud-bigquery`, `clickhouse-connect`, `mysql-connector-python`,
`duckdb`, `adbc-driver-postgresql`), so this image carries the largest
third-party tree of the three.

### `ralforion/orionbelt-semantic-layer-ui`

Built from `Dockerfile.ui` with the `ui` extra. It proxies execution to the
API, so it carries no drivers and no converter.

| Package | Licence |
|---|---|
| gradio | Apache-2.0 |
| pyarrow | Apache-2.0 |

### Transitive dependencies

Not enumerated here — including the vendor client SDKs the drivers pull in, and
everything `gradio` brings with it. They are resolved and pinned in
[`uv.lock`](uv.lock), which records the exact version of every package in a
build, and each carries its own licence in its distribution metadata.

For an authoritative per-image list, read the metadata inside the image rather
than trusting this file to stay current:

```bash
docker run --rm ralforion/orionbelt-semantic-layer-api \
  python -c "import importlib.metadata as m; [print(d.name, d.version) for d in m.distributions()]"
```

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
