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
    """Build a self-contained vis-network HTML graph from OBML model YAML."""
    import json
    import re
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

    if show_data_objects:
        for obj_name, obj in model.data_objects.items():
            nid = f"do_{obj_name}"
            title = f"DataObject: {obj_name}\nTable: {obj.qualified_code}"
            if obj.description:
                title += f"\n{obj.description}"
            add_node(
                nid,
                label=obj_name,
                title=title,
                color={"background": "#9E9E9E", "border": "#757575"},
                shape="box",
                size=30,
            )

        if show_joins:
            for obj_name, obj in model.data_objects.items():
                for join in obj.joins:
                    style: dict[str, object] = {
                        "label": join.join_type.value,
                        "title": f"{obj_name} → {join.join_to}\n{join.join_type.value}",
                        "color": "#BDBDBD",
                        "arrows": "to",
                    }
                    if join.secondary:
                        style["dashes"] = True
                        lbl = join.path_name or "secondary"
                        style["label"] = lbl
                        style["title"] = f"{obj_name} → {join.join_to}\n{lbl} (secondary)"
                    add_edge(f"do_{obj_name}", f"do_{join.join_to}", **style)

    if show_dimensions:
        for dim_name, dim in model.dimensions.items():
            nid = f"dim_{dim_name}"
            title = (
                f"Dimension: {dim_name}\nDataObject: {dim.view}"
                f"\nColumn: {dim.column}\nType: {dim.result_type.value}"
            )
            if dim.via:
                title += f"\nVia: {dim.via}"
            if dim.description:
                title += f"\n{dim.description}"
            add_node(
                nid,
                label=dim_name,
                title=title,
                color={"background": "#4CAF50", "border": "#388E3C"},
                shape="box",
                size=20,
            )
            if show_data_objects and f"do_{dim.view}" in node_ids:
                add_edge(
                    nid,
                    f"do_{dim.view}",
                    label="dataObject",
                    title=f"{dim_name} → {dim.view}",
                    color="#4CAF50",
                    arrows="to",
                )
            if dim.via and show_data_objects and f"do_{dim.via}" in node_ids:
                add_edge(
                    nid,
                    f"do_{dim.via}",
                    label="via",
                    title=f"{dim_name} via {dim.via}",
                    color="#81C784",
                    arrows="to",
                    dashes=True,
                )

    if show_measures:
        # effective_measures includes auto-synthesized row-count measures (e.g.
        # "Sales Count") so they appear in the graph like declared measures.
        for meas_name, meas in model.effective_measures.items():
            nid = f"meas_{meas_name}"
            title = (
                f"Measure: {meas_name}\nAggregation: {meas.aggregation}"
                f"\nType: {meas.result_type.value}"
            )
            if meas.expression:
                title += f"\nExpression: {meas.expression}"
            if meas.description:
                title += f"\n{meas.description}"
            add_node(
                nid,
                label=meas_name,
                title=title,
                color={"background": "#2196F3", "border": "#1976D2"},
                shape="box",
                size=20,
            )
            seen: set[str] = set()
            for ref in meas.columns:
                if ref.view and ref.view not in seen:
                    seen.add(ref.view)
                    if show_data_objects and f"do_{ref.view}" in node_ids:
                        # Synthesized counts are column-less (anchored to the
                        # object grain), so label the edge accordingly.
                        anchored = ref.column is None
                        add_edge(
                            nid,
                            f"do_{ref.view}",
                            label="anchor" if anchored else "sourceColumn",
                            title=(
                                f"{meas_name} anchored to {ref.view}"
                                if anchored
                                else f"{meas_name} → {ref.view}.{ref.column}"
                            ),
                            color="#64B5F6",
                            arrows="to",
                        )
            # Expression-based measures reference columns via their formula
            # (e.g. "{[Orders].[Price]} * {[Orders].[Quantity]}"); link those to
            # the referenced objects. Labeled "referencesColumn" to match the
            # ontology predicate (distinct from declared-column "sourceColumn").
            if meas.expression:
                for obj_name, _col in re.findall(
                    r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}", meas.expression
                ):
                    if obj_name not in seen:
                        seen.add(obj_name)
                        if show_data_objects and f"do_{obj_name}" in node_ids:
                            add_edge(
                                nid,
                                f"do_{obj_name}",
                                label="referencesColumn",
                                title=f"{meas_name} → {obj_name}",
                                color="#64B5F6",
                                arrows="to",
                            )

    if show_metrics:
        for met_name, met in model.metrics.items():
            nid = f"met_{met_name}"
            title = f"Metric: {met_name}\nType: {met.type.value}"
            if met.expression:
                title += f"\nExpression: {met.expression}"
            if met.description:
                title += f"\n{met.description}"
            add_node(
                nid,
                label=met_name,
                title=title,
                color={"background": "#9C27B0", "border": "#7B1FA2"},
                shape="box",
                size=20,
            )
            if met.expression and show_measures:
                refs = re.findall(r"\{\[([^\]]+)\]\}", met.expression)
                for ref_name in refs:
                    if f"meas_{ref_name}" in node_ids:
                        add_edge(
                            nid,
                            f"meas_{ref_name}",
                            label="referencesMeasure",
                            title=f"{met_name} → {ref_name}",
                            color="#CE93D8",
                            arrows="to",
                        )
            if met.measure and show_measures and f"meas_{met.measure}" in node_ids:
                add_edge(
                    nid,
                    f"meas_{met.measure}",
                    label="baseMeasure",
                    title=f"{met_name} → {met.measure}",
                    color="#CE93D8",
                    arrows="to",
                )

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
