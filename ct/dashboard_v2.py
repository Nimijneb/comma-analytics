"""The dashboard in Grafana's v2 format (dashboard.grafana.app/v2), which rows that hide themselves when empty need.

setup_grafana builds the classic JSON (panels with absolute grid positions, rows as full-width markers); to_v2()
turns it into v2, matching Grafana's own conversion. flat() reads a v2 dashboard's layout back into classic
positions, so keep_layout can carry layout edits made in Grafana over to the next setup."""

DEFAULT_REFRESH_INTERVALS = ["5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h", "2h", "1d"]
HIDE = {0: "dontHide", 1: "hideLabel", 2: "hideVariable"}
REFRESH = {0: "never", 1: "onDashboardLoad", 2: "onTimeRangeChanged"}
SORT = {0: "disabled", 1: "alphabeticalAsc", 2: "alphabeticalDesc", 3: "numericalAsc", 4: "numericalDesc",
        5: "alphabeticalCaseInsensitiveAsc", 6: "alphabeticalCaseInsensitiveDesc", 7: "naturalAsc", 8: "naturalDesc"}
GRID_CELL, GRID_GAP = 30, 8  # px per classic grid unit, and between units
CURSOR_SYNC = {0: "Off", 1: "Crosshair", 2: "Tooltip"}
QUERY_OPTIONS = ("maxDataPoints", "interval", "timeFrom", "timeShift", "cacheTimeout", "queryCachingTTL", "hideTimeOverride")
# shown only while the panel has data (Grafana can only check this on a panel in an auto grid, not on a row)
WHEN_DATA = {"kind": "ConditionalRenderingGroup",
             "spec": {"visibility": "show", "condition": "and", "items": [{"kind": "ConditionalRenderingData", "spec": {"value": True}}]}}


def data_query(ds: dict, spec: dict) -> dict:
  return {"kind": "DataQuery", "group": ds["type"], "version": "v0", "datasource": {"name": ds["uid"]}, "spec": spec}


def element(p: dict) -> dict:
  if "_v2" in p:  # a panel added in Grafana: kept as it was
    return p["_v2"]
  queries = [{"kind": "PanelQuery", "spec": {
               "query": data_query(t.get("datasource") or p["datasource"], {k: v for k, v in t.items() if k not in ("refId", "datasource", "hide")}),
               "refId": t.get("refId", "A"), "hidden": bool(t.get("hide"))}}
             for t in p.get("targets", [])]
  spec = {"id": p["id"], "title": p.get("title", "")}
  if p.get("description"):
    spec["description"] = p["description"]
  spec |= {"links": p.get("links", []),
           "data": {"kind": "QueryGroup", "spec": {
             "queries": queries,
             "transformations": [{"kind": "Transformation", "group": t["id"], "spec": {"options": t.get("options", {})}}
                                 for t in p.get("transformations", [])],
             "queryOptions": {k: p[k] for k in QUERY_OPTIONS if k in p}}},
           "vizConfig": {"kind": "VizConfig", "group": p["type"], "version": p.get("pluginVersion", ""),
                         "spec": {"options": p.get("options", {}), "fieldConfig": p.get("fieldConfig", {"defaults": {}, "overrides": []})}}}
  if p.get("transparent"):
    spec["transparent"] = True
  return {"kind": "Panel", "spec": spec}


def variable(v: dict) -> dict:
  if v["type"] != "query":
    raise ValueError(f"variable {v['name']}: type {v['type']} isn't converted")
  current = {k: x for k, x in v.get("current", {}).items() if k != "selected"} or {"text": "", "value": ""}
  spec = {"name": v["name"], "current": current}
  if "label" in v:
    spec["label"] = v["label"]
  spec |= {"hide": HIDE[v.get("hide", 0)], "refresh": REFRESH[v.get("refresh", 1)], "skipUrlSync": bool(v.get("skipUrlSync")),
           "query": data_query(v["datasource"], v["query"]), "regex": v.get("regex", ""), "sort": SORT[v.get("sort", 0)],
           "options": [], "multi": bool(v.get("multi")), "includeAll": bool(v.get("includeAll")), "allowCustomValue": True}
  if "allValue" in v:
    spec["allValue"] = v["allValue"]
  return {"kind": "QueryVariable", "spec": spec}


def annotation(a: dict) -> dict:
  spec = {"query": data_query(a["datasource"], a["target"]), "enable": bool(a.get("enable")), "hide": bool(a.get("hide")),
          "iconColor": a["iconColor"], "name": a["name"]}
  if "filter" in a:
    spec["filter"] = {"ids": a["filter"]["ids"]} | ({"exclude": True} if a["filter"].get("exclude") else {})
  if "mappings" in a:
    spec["mappings"] = a["mappings"]
  return {"kind": "AnnotationQuery", "spec": spec}


BUILT_IN_ANNOTATIONS = {"kind": "AnnotationQuery", "spec": {
  "query": {"kind": "DataQuery", "group": "grafana", "version": "v0", "datasource": {"name": "-- Grafana --"}, "spec": {}},
  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "builtIn": True,
  "legacyOptions": {"type": "dashboard"}}}


def grid(panels: list[dict], top: int) -> dict:
  return {"kind": "GridLayout", "spec": {"items": [
    {"kind": "GridLayoutItem", "spec": {"x": p["gridPos"]["x"], "y": p["gridPos"]["y"] - top, "width": p["gridPos"]["w"],
                                        "height": p["gridPos"]["h"], "element": {"kind": "ElementReference", "name": f"panel-{p['id']}"}}}
    for p in sorted(panels, key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))]}}


def auto_grid(panels: list[dict]) -> dict:
  """Panels side by side in an auto grid, each hidden while it has no data; the grid is as tall as the tallest."""
  h = max(p["gridPos"]["h"] for p in panels)
  return {"kind": "AutoGridLayout", "spec": {
    "maxColumnCount": len(panels), "columnWidthMode": "custom", "columnWidth": 100,
    "rowHeightMode": "custom", "rowHeight": h * GRID_CELL + (h - 1) * GRID_GAP,
    "items": [{"kind": "AutoGridLayoutItem", "spec": {"element": {"kind": "ElementReference", "name": f"panel-{p['id']}"},
                                                       "conditionalRendering": WHEN_DATA}}
              for p in sorted(panels, key=lambda p: p["gridPos"]["x"])]}}


def row_layout(panels: list[dict], top: int, hide_empty: list[set]) -> dict:
  """A row's panels as one grid, or, when it holds a hide-when-empty group, as headerless sub-rows: the grid above
  the group, the group (shown only when it has data), the grid below, and so on."""
  bands = []
  for group in hide_empty:
    members = [p for p in panels if p["id"] in group]
    if not members:
      continue
    start = min(p["gridPos"]["y"] for p in members)
    end = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in members)
    others = [p for p in panels if p["id"] not in group and p["gridPos"]["y"] < end and p["gridPos"]["y"] + p["gridPos"]["h"] > start]
    if others:  # something sits beside the group: it can't get a row of its own, so it just stays put
      print(f"panels {sorted(group)} share rows with panel {others[0]['id']}: not hiding them when empty")
      continue
    bands.append((start, end, group))
  if not bands:
    return grid(panels, top)
  rows, cut = [], top
  for start, end, group in sorted(bands, key=lambda b: b[0]):
    above = [p for p in panels if cut <= p["gridPos"]["y"] < start and p["id"] not in group]
    if above:
      rows.append({"kind": "RowsLayoutRow", "spec": {"title": "", "collapse": False, "hideHeader": True, "layout": grid(above, cut)}})
    rows.append({"kind": "RowsLayoutRow", "spec": {"title": "", "collapse": False, "hideHeader": True,
                                                   "layout": auto_grid([p for p in panels if p["id"] in group])}})
    cut = end
  below = [p for p in panels if p["gridPos"]["y"] >= cut]
  if below:
    rows.append({"kind": "RowsLayoutRow", "spec": {"title": "", "collapse": False, "hideHeader": True, "layout": grid(below, cut)}})
  return {"kind": "RowsLayout", "spec": {"rows": rows}}


def to_v2(dash: dict, hide_empty: list[set] = ()) -> dict:
  """The v2 spec of a classic dashboard. `hide_empty`: groups of panel ids, each in full-width rows of its own,
  that are hidden while none of them has data."""
  rows, loose = [], []
  # a panel on the same line as a row goes above it
  for p in sorted(dash["panels"], key=lambda p: (p["gridPos"]["y"], p["type"] == "row", p["gridPos"]["x"])):
    if p["type"] == "row":
      rows.append((p, list(p.get("panels", []))))
    else:
      (rows[-1][1] if rows else loose).append(p)
  if loose:  # panels above the first row go in a headerless one
    rows.insert(0, ({"title": "", "gridPos": {"y": -1}, "_headerless": True}, loose))
  every = [p for _, ps in rows for p in ps]
  layout_rows = []
  for r, ps in rows:
    spec = {"title": r.get("title", ""), "collapse": bool(r.get("collapsed"))}
    if r.get("_headerless"):
      spec["hideHeader"] = True
    spec["layout"] = row_layout(ps, r["gridPos"]["y"] + 1, hide_empty)
    layout_rows.append({"kind": "RowsLayoutRow", "spec": spec})
  return {
    "annotations": [BUILT_IN_ANNOTATIONS] + [annotation(a) for a in dash.get("annotations", {}).get("list", [])],
    "cursorSync": CURSOR_SYNC[dash.get("graphTooltip", 0)],
    "editable": dash.get("editable", True),
    "elements": {f"panel-{p['id']}": element(p) for p in every},
    "layout": {"kind": "RowsLayout", "spec": {"rows": layout_rows}},
    "links": [{"title": l["title"], "type": l["type"], "icon": l.get("icon", "external link"), "tooltip": l.get("tooltip", ""),
               "url": l.get("url", ""), "tags": l.get("tags", []), "asDropdown": bool(l.get("asDropdown")),
               "targetBlank": bool(l.get("targetBlank")), "includeVars": bool(l.get("includeVars")), "keepTime": bool(l.get("keepTime"))}
              for l in dash.get("links", [])],
    "liveNow": False,
    "preload": False,
    "tags": dash.get("tags", []),
    "timeSettings": {"timezone": dash.get("timezone", "browser"), "from": dash["time"]["from"], "to": dash["time"]["to"],
                     "autoRefresh": dash.get("refresh", ""), "autoRefreshIntervals": DEFAULT_REFRESH_INTERVALS,
                     "hideTimepicker": False, "fiscalYearStartMonth": 0},
    "title": dash["title"],
    "variables": [variable(v) for v in dash.get("templating", {}).get("list", [])],
  }


def flat(spec: dict) -> dict:
  """A v2 spec's layout as a classic panel list (absolute positions, rows as markers; headerless rows take no
  space), each panel carrying its v2 element as `_v2`."""
  panels, row_id = [], [10_000]

  def walk(layout: dict, top: int) -> int:
    """Add `layout`'s panels from grid row `top` down; return the row below it."""
    if layout["kind"] == "GridLayout":
      bottom = top
      for item in layout["spec"]["items"]:
        s = item["spec"]
        name = s["element"]["name"]
        el = spec["elements"].get(name)
        if el is None or el["kind"] != "Panel":
          continue
        panels.append({"id": el["spec"]["id"], "type": el["spec"]["vizConfig"]["group"], "title": el["spec"].get("title", ""),
                       "gridPos": {"x": s["x"], "y": top + s["y"], "w": s["width"], "h": s["height"]}, "_v2": el})
        bottom = max(bottom, top + s["y"] + s["height"])
      return bottom
    if layout["kind"] == "AutoGridLayout":  # one line of equal-width panels (as auto_grid writes them)
      items = [spec["elements"].get(i["spec"]["element"]["name"]) for i in layout["spec"]["items"]]
      items = [el for el in items if el and el["kind"] == "Panel"]
      h = round((layout["spec"].get("rowHeight", 320) + GRID_GAP) / (GRID_CELL + GRID_GAP))
      for i, el in enumerate(items):
        panels.append({"id": el["spec"]["id"], "type": el["spec"]["vizConfig"]["group"], "title": el["spec"].get("title", ""),
                       "gridPos": {"x": i * 24 // len(items), "y": top, "w": 24 // len(items), "h": h}, "_v2": el})
      return top + h if items else top
    if layout["kind"] == "RowsLayout":
      for r in layout["spec"]["rows"]:
        s = r["spec"]
        if not s.get("hideHeader"):
          row_id[0] += 1
          panels.append({"id": row_id[0], "type": "row", "title": s.get("title", ""), "collapsed": bool(s.get("collapse")),
                         "gridPos": {"x": 0, "y": top, "w": 24, "h": 1}, "panels": []})
          top += 1
        top = walk(s["layout"], top)
      return top
    raise ValueError(f"layout {layout['kind']} isn't read back")

  walk(spec["layout"], 0)
  return {"panels": panels}
