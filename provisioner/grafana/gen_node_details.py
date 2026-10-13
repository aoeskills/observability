# Adapted from the GCP ~/monitoring generator for this stack: Grafana reads metrics from OpenObserve's Prometheus API
# (no round(x, 0.1): OpenObserve rejects it), served at the root path, macOS memory supported.
"""Build node-exporter-details.json from the stock 1860 JSON.

Only change: the CPU Busy / RAM Used / Root FS Used gauges take their warn/crit thresholds
from PostgreSQL (table usage_threshold): the machine's own row, else the '*' default row.
"""
import copy
import json
import sys

import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import o2_links  # noqa: E402 — links to OpenObserve traces / logs for the current time range

SRC = sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent / "node-exporter-full-1860.json"
GAUGES = {20: "cpu", 16: "memory", 154: "disk"}  # panel id -> resource
PG = {"type": "grafana-postgresql-datasource", "uid": "thresholds-db"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}

SQL = """SELECT COALESCE(t.warn, d.warn) AS warn, COALESCE(t.crit, d.crit) AS crit
FROM (SELECT 1) AS x
LEFT JOIN usage_threshold d ON d.instance = '*' AND d.resource = '{res}'
LEFT JOIN usage_threshold t ON t.instance = ${{node:sqlstring}} AND t.resource = '{res}'"""

d = json.load(open(SRC))
d["uid"] = "node-exporter-details"
d["title"] = "Node Exporter Details"
d["id"] = None
d["version"] = 1


def walk(panels):
    for p in panels:
        yield p
        if p.get("panels"):
            yield from walk(p["panels"])


seen = set()
for p in walk(d["panels"]):
    res = GAUGES.get(p.get("id"))
    if not res or p["type"] != "gauge":
        continue
    seen.add(p["id"])
    prom_ds = copy.deepcopy(p["targets"][0].get("datasource") or p.get("datasource"))
    for t in p["targets"]:
        t["datasource"] = copy.deepcopy(prom_ds)
    p["datasource"] = copy.deepcopy(MIXED)
    p["targets"].append({
        "refId": "TH", "datasource": copy.deepcopy(PG), "format": "table", "rawQuery": True,
        "editorMode": "code", "rawSql": SQL.format(res=res),
    })
    p["transformations"] = [
        {"id": "configFromData", "options": {
            "configRefId": "TH",
            "mappings": [{"fieldName": "warn", "handlerKey": "threshold1"},
                         {"fieldName": "crit", "handlerKey": "threshold2"}]}},
    ]

assert seen == set(GAUGES), f"gauges not found: {set(GAUGES) - seen}"

# macOS has no MemAvailable: RAM Used falls back to (active + wired + compressed) / total
for p in walk(d["panels"]):
    if p.get("id") == 16 and p["type"] == "gauge":
        t = p["targets"][0]
        t["expr"] = (t["expr"].strip() + ' or (node_memory_active_bytes{instance="$node", job="$job"}'
                     ' + node_memory_wired_bytes{instance="$node", job="$job"} + node_memory_compressed_bytes{instance="$node", job="$job"})'
                     ' / node_memory_total_bytes{instance="$node", job="$job"} * 100')

# ---- variables: list every scraped machine (incl. ones that are down / never answered), not just those with node_uname_info.
# A machine's instance label is its name (host.name, set by its agent), so the dropdown shows it as is.
for v in d["templating"]["list"]:
    if v["name"] == "job":
        q = 'label_values(up{job="node"}, job)'  # machines (agents' node_exporter scrape), not the gateway's own jobs
        v["definition"] = q
        v["query"] = {"query": q, "refId": "Prometheus-job-Variable-Query"}
    elif v["name"] == "node":
        q = 'label_values(up{job="$job"}, instance)'
        v["definition"] = q
        v["query"] = {"query": q, "refId": "Prometheus-node-Variable-Query"}
        v["regex"] = ""
        v["label"] = "Machine"
        v["sort"] = 1
    elif v["name"] == "nodename":
        v["hide"] = 2  # no panel uses it; Instance no longer depends on it

# ---- Availability section on top (works even when the machine is down)
PROM = {"type": "prometheus", "uid": "${ds_prometheus}"}
UPQ = 'up{job="$job",instance="$node"}'
UPDOWN = [{"type": "value", "options": {
    "1": {"text": "UP", "color": "green", "index": 0},
    "0": {"text": "DOWN", "color": "red", "index": 1}}}]


def stat(pid, title, expr, x, unit, steps, decimals=None, no_value="no data", mappings=None, desc=""):
    p = {"id": pid, "type": "stat", "title": title, "description": desc, "datasource": PROM,
         "gridPos": {"x": x, "y": 1, "w": 6, "h": 4},
         "targets": [{"datasource": PROM, "refId": "A", "expr": expr, "instant": True, "range": False}],
         "fieldConfig": {"defaults": {"unit": unit, "noValue": no_value, "color": {"mode": "thresholds"},
                                      "thresholds": {"mode": "absolute", "steps": steps},
                                      "mappings": mappings or []}, "overrides": []},
         "options": {"colorMode": "background", "graphMode": "none", "textMode": "value",
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def ts(pid, title, expr, x, unit, desc, ymax=None):
    dfl = {"unit": unit, "min": 0, "custom": {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never"}}
    if ymax:
        dfl["max"] = ymax
    return {"id": pid, "type": "timeseries", "title": title, "description": desc, "datasource": PROM,
            "gridPos": {"x": x, "y": 10, "w": 12, "h": 7},
            "targets": [{"datasource": PROM, "refId": "A", "expr": expr, "legendFormat": "{{instance}}"}],
            "fieldConfig": {"defaults": dfl, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}


GR = [{"color": "green", "value": None}]
new_panels = [
    {"id": 9001, "type": "row", "title": "Availability", "collapsed": False, "panels": [],
     "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
    stat(9002, "Status now", UPQ, 0, "none",
         [{"color": "red", "value": None}, {"color": "green", "value": 1}], mappings=UPDOWN, no_value="no data"),
    stat(9003, "Availability (selected range)", f'100 * avg_over_time({UPQ}[$__range])', 6, "percent",
         [{"color": "red", "value": None}, {"color": "orange", "value": 95}, {"color": "green", "value": 99.9}], decimals=2,
         desc="Share of scrapes that succeeded in the selected time range."),
    stat(9004, "Downtime (selected range)", f'(1 - avg_over_time({UPQ}[$__range])) * $__range_s', 12, "s",
         [{"color": "green", "value": None}, {"color": "red", "value": 1}], decimals=0,
         desc="Estimated time the machine did not answer, from the share of failed scrapes."),
    stat(9005, "Last seen up", f'time() - max_over_time(timestamp({UPQ} == 1)[$__range:15s])', 18, "s",
         [{"color": "green", "value": None}, {"color": "orange", "value": 60}, {"color": "red", "value": 300}],
         decimals=0, no_value="never in this range",
         desc="How long ago a scrape last succeeded. 'never in this range' = no successful scrape in the selected time range."),
    {"id": 9006, "type": "state-timeline", "title": "Up / Down over time", "datasource": PROM,
     "gridPos": {"x": 0, "y": 5, "w": 24, "h": 5},
     "targets": [{"datasource": PROM, "refId": "A", "expr": UPQ, "legendFormat": "{{instance}}"}],
     "fieldConfig": {"defaults": {"mappings": UPDOWN, "color": {"mode": "thresholds"},
                                  "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]},
                                  "custom": {"fillOpacity": 85, "lineWidth": 0}}, "overrides": []},
     "options": {"showValue": "never", "rowHeight": 0.8, "mergeValues": True, "legend": {"showLegend": False}}},
    ts(9007, "Scrape duration", f'scrape_duration_seconds{{job="$job",instance="$node"}}', 0, "s",
       "How long the collector waited for the machine. Flat at ~10 s = timeout (host down, network or firewall). Near 0 s = refused immediately or DNS failure."),
    ts(9008, "Samples scraped", f'scrape_samples_scraped{{job="$job",instance="$node"}}', 12, "short",
       "Number of metrics returned per scrape. 0 = the scrape failed."),
]
new_panels.append({
    "id": 9010, "type": "stat", "title": "Name", "description": "The machine's name (host.name, HOST_NAME in its agent/.env).",
    "datasource": PROM, "gridPos": {"x": 0, "y": 1, "w": 6, "h": 4},
    "targets": [{"datasource": PROM, "refId": "A", "instant": True, "range": False, "legendFormat": "{{instance}}",
                 "expr": 'up{job="$job",instance="$node"}'}],
    "fieldConfig": {"defaults": {"noValue": "no data", "color": {"mode": "fixed", "fixedColor": "blue"}}, "overrides": []},
    "options": {"colorMode": "none", "graphMode": "none", "textMode": "name",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}})
LAYOUT = {9010: (0, 6), 9002: (6, 4), 9003: (10, 5), 9004: (15, 5), 9005: (20, 4)}
for _p in new_panels:
    if _p["id"] in LAYOUT:
        _p["gridPos"]["x"], _p["gridPos"]["w"] = LAYOUT[_p["id"]]
SHIFT = 17  # 1 row + 4 + 5 + 7
for p in d["panels"]:
    p["gridPos"]["y"] += SHIFT
    for c in p.get("panels", []) or []:
        c["gridPos"]["y"] += SHIFT
d["panels"] = new_panels + d["panels"]
d["links"] = [l for l in d.get("links", []) if "OpenObserve" not in l.get("title", "")] + o2_links.links()
d["version"] = 4
print(json.dumps(d, indent=2))
