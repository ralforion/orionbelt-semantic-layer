"""Graph / diagram / SQL rendering helpers for the Gradio UI.

Pure presentation: local Mermaid ER generation, the self-contained
vis-network ontology graph, SQL passthrough formatting, and convert-status
formatting. None of these reach the network; the API-backed fetches live in
``orionbelt.ui.api_client``.
"""

from __future__ import annotations

from typing import Any

_VIS_NETWORK_B64: str | None = None


def _get_vis_network_b64() -> str:
    """Return base64-encoded vis-network.min.js (cached)."""
    import base64
    from pathlib import Path

    global _VIS_NETWORK_B64  # noqa: PLW0603
    if _VIS_NETWORK_B64 is None:
        js_path = Path(__file__).parent / "static" / "vis-network.min.js"
        js_bytes = js_path.read_bytes()
        _VIS_NETWORK_B64 = base64.b64encode(js_bytes).decode("ascii")
    return _VIS_NETWORK_B64


def _format_convert_status(
    direction: str,
    warnings: list[str],
    validation: dict[str, Any],
) -> str:
    """Build status lines from a /convert API response."""
    lines: list[str] = [direction]
    for w in warnings:
        lines.append(f"WARNING: {w}")
    schema_ok = (
        "✓"
        if validation.get("schema_valid", True)
        else (f"{len(validation.get('schema_errors', []))} error(s)")
    )
    sem_ok = (
        "✓"
        if validation.get("semantic_valid", True)
        else (f"{len(validation.get('semantic_errors', []))} error(s)")
    )
    lines.append(f"Validation: JSON Schema {schema_ok} | Semantic {sem_ok}")
    for e in validation.get("schema_errors", []):
        lines.append(f"Schema error: {e}")
    for e in validation.get("semantic_errors", []):
        lines.append(f"Semantic error: {e}")
    for w in validation.get("semantic_warnings", []):
        lines.append(f"Validation warning: {w}")
    return "\n".join(lines)


def _format_sql(sql: str) -> str:
    """Return SQL unchanged — the API now pretty-prints with sqlglot."""
    return sql


def _generate_mermaid_er_local(
    model_yaml: str, show_columns: bool = True, *, theme: str = "dark"
) -> tuple[str, str]:
    """Generate a Mermaid ER diagram locally from raw OBML YAML (no API).

    Returns ``(markdown, raw_mermaid)``."""
    from orionbelt.parser.loader import TrackedLoader
    from orionbelt.parser.resolver import ReferenceResolver
    from orionbelt.service.diagram import generate_mermaid_er

    try:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(model_yaml)
        resolver = ReferenceResolver()
        model, result = resolver.resolve(raw, source_map)
        if not result.valid:
            msgs = "; ".join(e.message for e in result.errors)
            return f"**Model validation failed:** {msgs}", ""
        mermaid = generate_mermaid_er(model, show_columns=show_columns, theme=theme)
        return f"```mermaid\n{mermaid}\n```", mermaid
    except Exception as exc:
        return f"**Error:** {exc}", ""


def _generate_ontology_graph_html(
    model_yaml: str,
    show_data_objects: bool = True,
    show_dimensions: bool = True,
    show_measures: bool = True,
    show_metrics: bool = True,
    show_joins: bool = True,
    node_spacing: int = 150,
) -> str:
    """Build a self-contained vis-network HTML graph from OBML model YAML.

    The graph is rendered *from the OBSL ontology*: the model is exported to an
    RDF graph and its individuals/predicates drive the nodes/edges, so the graph
    and the exported ontology never drift.
    """
    import json
    from collections import defaultdict

    from orionbelt.parser.loader import TrackedLoader
    from orionbelt.parser.resolver import ReferenceResolver

    if not model_yaml or not model_yaml.strip():
        return "<p style='padding:16px;opacity:0.6'>No model loaded.</p>"

    try:
        raw, source_map = TrackedLoader().load_string(model_yaml)
        model, result = ReferenceResolver().resolve(raw, source_map)
        if not result.valid:
            msgs = "; ".join(e.message for e in result.errors)
            return f"<p style='color:#F44336;padding:16px'>Model validation failed: {msgs}</p>"
    except Exception as exc:
        return f"<p style='color:#F44336;padding:16px'>Error: {exc}</p>"

    # The graph is a rendering of the OBSL ontology (single source of truth):
    # build the RDF graph, then map its individuals to nodes and its predicates
    # to edges. This keeps the graph and the exported ontology in lock-step —
    # a fix in the exporter shows up here for free.
    from rdflib import RDF, RDFS, Literal, URIRef

    from orionbelt.obsl.exporter import OBSL, export_obsl

    g = export_obsl(model, "graph")

    def _label(uri: Any) -> str:
        for o in g.objects(uri, RDFS.label):
            return str(o)
        return str(uri).rsplit("/", 1)[-1]

    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    node_ids: set[str] = set()

    def add_node(nid: str, **kwargs: object) -> None:
        if nid not in node_ids:
            node_ids.add(nid)
            kwargs["id"] = nid
            nodes.append(kwargs)

    def add_edge(src: str, tgt: str, **kwargs: object) -> None:
        kwargs["from"] = src
        kwargs["to"] = tgt
        edges.append(kwargs)

    # Nodes: one per obsl individual, coloured by type, gated by the filters.
    # (obsl:Column individuals are intentionally not shown — a measure's
    # sourceColumn is collapsed onto its owning data object below.)
    _node_types = [
        (OBSL.DataObject, "DataObject", show_data_objects, "#9E9E9E", "#757575", 30),
        (OBSL.Dimension, "Dimension", show_dimensions, "#4CAF50", "#388E3C", 20),
        (OBSL.Measure, "Measure", show_measures, "#2196F3", "#1976D2", 20),
        (OBSL.Metric, "Metric", show_metrics, "#9C27B0", "#7B1FA2", 20),
    ]

    def _node_title(uri: Any, kind: str) -> str:
        parts = [f"{kind}: {_label(uri)}"]
        for pred, lbl in (
            (OBSL.code, "Table"),
            (OBSL.aggregation, "Aggregation"),
            (OBSL.resultType, "Type"),
            (OBSL.metricType, "Metric type"),
            (OBSL.expressionSource, "Expression"),
        ):
            val = next(iter(g.objects(uri, pred)), None)
            if val is not None:
                parts.append(f"{lbl}: {val}")
        comment = next(iter(g.objects(uri, RDFS.comment)), None)
        if comment is not None:
            parts.append(str(comment))
        return "\n".join(parts)

    for cls, kind, shown, bg, border, size in _node_types:
        if not shown:
            continue
        for subj in g.subjects(RDF.type, cls):
            add_node(
                str(subj),
                label=_label(subj),
                title=_node_title(subj, kind),
                color={"background": bg, "border": border},
                shape="box",
                size=size,
            )

    # Map each obsl:Column to its owning data object so measure sourceColumn
    # edges can collapse onto the object node.
    col_to_object: dict[str, str] = {
        str(col): str(obj) for obj, col in g.subject_objects(OBSL.hasColumn)
    }

    seen_edges: set[tuple[str, str, str]] = set()

    def link(src: Any, tgt: Any, label: str, color: str, *, dashes: bool = False) -> None:
        s, t = str(src), str(tgt)
        if s not in node_ids or t not in node_ids:
            return
        key = (s, t, label)
        if key in seen_edges:
            return
        seen_edges.add(key)
        style: dict[str, object] = {
            "label": label,
            "title": f"{_label(src)} → {_label(tgt)}",
            "color": color,
            "arrows": "to",
        }
        if dashes:
            style["dashes"] = True
        add_edge(s, t, **style)

    # Dimension → data object (and via).
    for dim, obj in g.subject_objects(OBSL.dataObject):
        link(dim, obj, "dataObject", "#4CAF50")
    for dim, obj in g.subject_objects(OBSL.via):
        link(dim, obj, "via", "#81C784", dashes=True)

    # Measure → data object: declared sourceColumn and expression-referenced
    # referencesColumn both collapse onto the column's owning object; plus the
    # grain anchor for column-less (e.g. synthesized count) measures.
    for pred, edge_label in (
        (OBSL.sourceColumn, "sourceColumn"),
        (OBSL.referencesColumn, "referencesColumn"),
    ):
        for meas, col in g.subject_objects(pred):
            obj_id = col_to_object.get(str(col))
            if obj_id is not None:
                link(meas, URIRef(obj_id), edge_label, "#64B5F6")
    for meas, obj in g.subject_objects(OBSL.anchoredTo):
        link(meas, obj, "anchor", "#64B5F6")

    # Metric → measure.
    for met, meas in g.subject_objects(OBSL.referencesMeasure):
        link(met, meas, "referencesMeasure", "#CE93D8")
    for met, meas in g.subject_objects(OBSL.baseMeasure):
        link(met, meas, "baseMeasure", "#CE93D8")

    # Joins: obsl:Join individuals become data-object → data-object edges.
    if show_joins:
        for join in g.subjects(RDF.type, OBSL.Join):
            targets = list(g.objects(join, OBSL.joinTo))
            card = next(iter(g.objects(join, OBSL.cardinality)), None)
            path = next(iter(g.objects(join, OBSL.pathName)), None)
            secondary = (join, OBSL.secondary, Literal(True)) in g
            label = str(path) if (secondary and path) else (str(card) if card else "join")
            for src in g.subjects(OBSL.hasJoin, join):
                for tgt in targets:
                    if isinstance(tgt, URIRef):
                        link(src, tgt, label, "#BDBDBD", dashes=secondary)

    edge_groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for edge in edges:
        a, b = sorted((str(edge["from"]), str(edge["to"])))
        edge_groups[(a, b)].append(edge)
    for group in edge_groups.values():
        if len(group) < 2:
            continue
        for i, edge in enumerate(group):
            if i == 0:
                edge["smooth"] = {"enabled": True, "type": "curvedCW", "roundness": 0.2}
            elif i % 2 == 1:
                edge["smooth"] = {
                    "enabled": True,
                    "type": "curvedCCW",
                    "roundness": 0.2 * ((i + 1) // 2),
                }
            else:
                edge["smooth"] = {
                    "enabled": True,
                    "type": "curvedCW",
                    "roundness": 0.2 * ((i + 1) // 2),
                }

    n_count = len(nodes)
    iters = min(max(n_count * 3, 150), 500)
    options = {
        "physics": {
            "enabled": True,
            "barnesHut": {
                "gravitationalConstant": -5000,
                "centralGravity": 0.3,
                "springLength": node_spacing,
                "springConstant": 0.04,
                "avoidOverlap": 0.3,
            },
            "stabilization": {"enabled": True, "iterations": iters},
        },
        "nodes": {"font": {"color": "#f0f0f0", "size": 12}},
        "edges": {
            "font": {
                "color": "#cccccc",
                "size": 10,
                "strokeWidth": 2,
                "strokeColor": "#222222",
            },
            "smooth": {"enabled": True, "type": "curvedCW", "roundness": 0.2},
        },
    }

    nodes_json = json.dumps(nodes)
    edges_json = json.dumps(edges)
    options_json = json.dumps(options)

    vis_b64 = _get_vis_network_b64()

    inner_html = f"""<!DOCTYPE html>
<html><head><style>
html,body{{margin:0;padding:0;overflow:hidden;background:transparent;width:100%;height:100%}}
#g{{width:100%;height:100%}}
#gtoolbar{{position:absolute;top:8px;right:8px;z-index:10;display:flex;gap:6px}}
.gbtn{{background:rgba(128,128,128,0.35);border:1px solid rgba(200,200,200,0.35);
border-radius:6px;padding:6px 10px;cursor:pointer;color:#fff;font-size:18px;line-height:1;
display:inline-flex;align-items:center;justify-content:center}}
.gbtn:hover{{background:rgba(160,160,160,0.55)}}
#dl-btn{{background:#2196F3;border-color:#1976D2}}
#dl-btn:hover{{background:#1565C0}}
</style></head><body>
<div id="g"></div>
<div id="gtoolbar">
<button id="rot-l" class="gbtn" title="Rotate left">&#8634;</button>
<button id="rot-r" class="gbtn" title="Rotate right">&#8635;</button>
<button id="dl-btn" class="gbtn" title="Download as PNG">&#11123;</button>
</div>
<script>
var s=document.createElement('script');
s.textContent=atob('{vis_b64}');
document.head.appendChild(s);
var n=new vis.DataSet({nodes_json});
var e=new vis.DataSet({edges_json});
var o={options_json};
var nw=new vis.Network(document.getElementById('g'),
  {{nodes:n,edges:e}},o);
nw.once('stabilizationIterationsDone',function(){{
  nw.fit({{animation:false,padding:15}});
  setTimeout(function(){{nw.setOptions({{physics:{{enabled:false}}}});}},500);
}});
window.addEventListener('resize',function(){{nw.redraw();nw.fit({{padding:15}});}});
document.getElementById('dl-btn').onclick=function(){{
  var cv=document.querySelector('canvas');
  if(!cv)return;
  var a=document.createElement('a');
  a.href=cv.toDataURL('image/png');
  a.download='ontology-graph.png';
  a.click();
}};
function rotate(deg){{
  nw.setOptions({{physics:{{enabled:false}}}});
  var rad=deg*Math.PI/180,pos=nw.getPositions(),ids=Object.keys(pos);
  if(!ids.length)return;
  var cx=0,cy=0;
  ids.forEach(function(id){{cx+=pos[id].x;cy+=pos[id].y;}});
  cx/=ids.length;cy/=ids.length;
  var cs=Math.cos(rad),sn=Math.sin(rad);
  ids.forEach(function(id){{
    var dx=pos[id].x-cx,dy=pos[id].y-cy;
    nw.moveNode(id,cx+dx*cs-dy*sn,cy+dx*sn+dy*cs);
  }});
  nw.fit({{animation:false,padding:15}});
}}
document.getElementById('rot-l').onclick=function(){{rotate(-15);}};
document.getElementById('rot-r').onclick=function(){{rotate(15);}};
</script></body></html>"""

    srcdoc = inner_html.replace("&", "&amp;").replace('"', "&quot;")
    return (
        f'<iframe srcdoc="{srcdoc}" '
        f'style="width:100%;height:calc(100dvh - 310px);'
        f"border:1px solid #555;"
        f'border-radius:8px" sandbox="allow-scripts allow-downloads"></iframe>'
    )


def _render_ontology_graph(
    model_yaml: str,
    show_data_objects: bool,
    show_dimensions: bool,
    show_measures: bool,
    show_metrics: bool,
    show_joins: bool,
    node_spacing: int = 150,
) -> str:
    """Gradio callback for the Ontology Graph tab."""
    return _generate_ontology_graph_html(
        model_yaml,
        show_data_objects,
        show_dimensions,
        show_measures,
        show_metrics,
        show_joins,
        node_spacing=int(node_spacing),
    )
