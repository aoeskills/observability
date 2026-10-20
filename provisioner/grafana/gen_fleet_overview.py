# Grafana reads metrics from OpenObserve's Prometheus API
# (no round(x, 0.1): OpenObserve rejects it), served at the root path, macOS memory supported.
"""Fleet Overview: cards with per-card Edit button; thresholds live in PostgreSQL (usage_threshold)."""
import json
import sys

import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import o2_links  # noqa: E402 — links to OpenObserve traces / logs for the current time range

PROM = {"type": "prometheus", "uid": "${ds}"}
PG = {"type": "grafana-postgresql-datasource", "uid": "thresholds-db"}
MIXED = {"type": "datasource", "uid": "-- Mixed --"}
# Grafana is served under this sub path (Caddy, GF_SERVER_SERVE_FROM_SUB_PATH); only hand-written links need it, the JS uses appSubUrl
SUBPATH = "/grafana"
PLUGIN = "marcusolsson-dynamictext-panel"
P = 'service_namespace=~"$namespace"'  # OpenTelemetry service.namespace = the project

# Name shown for each machine = its instance label, which the machine's agent sets to its host.name (HOST_NAME in
# agent/.env). Read from the same series as the value, so a renamed machine shows its new name with the next sample
# (no separate name metric to join, which could keep an old name).
def named(expr):
    # label_join copies the value; OpenObserve's label_replace with "$1" / "(.*)" leaves the label unset (v1.0.4)
    return f'label_join({expr}, "name", "", "instance")'

# ---------------------------------------------------------------- queries
# CPU busy % = 1 - idle time / all CPU time, both over the same samples. Not `1 - avg(rate(idle))`: OpenObserve's rate()
# divides by the whole [5m] window even when it holds less data (an agent that just started or stopped), which showed
# 80-90 % right after an agent's start or stop (seen 2026-10-13); in the ratio that error cancels out. With a full window
# the denominator is the number of cores, so the value is the same as before.
CPU_BY = "instance,job,service_namespace"
CPU = (f'100 * (1 - sum by({CPU_BY})(rate(node_cpu_seconds_total{{mode="idle",{P}}}[5m]))'
       f' / sum by({CPU_BY})(rate(node_cpu_seconds_total{{{P}}}[5m])))')
# Linux has MemAvailable; macOS does not: there "used" = active + wired + compressed (what Activity Monitor shows).
# OpenObserve stores metric names lowercased (MemAvailable -> memavailable); the mixed-case name finds nothing.
MEM = (f'100 * (1 - node_memory_memavailable_bytes{{{P}}} / node_memory_memtotal_bytes{{{P}}})'
       f' or 100 * (node_memory_active_bytes{{{P}}} + node_memory_wired_bytes{{{P}}} + node_memory_compressed_bytes{{{P}}})'
       f' / node_memory_total_bytes{{{P}}}')
DSEL = f'mountpoint="/",fstype!~"tmpfs|rootfs|overlay",{P}'
DISK = (f'100 - 100 * max by(instance,job,service_namespace)(node_filesystem_avail_bytes{{{DSEL}}})'
        f' / max by(instance,job,service_namespace)(node_filesystem_size_bytes{{{DSEL}}})')


def card_series(expr, metric, order, resource):
    x = named(f'round(({expr}) * 10) / 10')
    for label, val in (("metric", metric), ("order", order), ("resource", resource)):
        x = f'label_replace({x}, "{label}", "{val}", "", "")'
    return x


# One Status card per machine: its `up` in the last minute (1 = UP, 0 = node_exporter not answering), plus a DOWN card (0) for
# every machine in the inventory (table machine, exported as machine_registered) that sent nothing in the last minute:
# agents push, so a dead machine is silent. That card has the Retire button. `+ 0` keeps an empty side from emptying the
# whole union (OpenObserve quirk, see gen_alerts.py).
NODE_UP = f'job="node",{P}'
LIVE = f'max_over_time(up{{{NODE_UP}}}[1m])'
REGISTERED = f'(max by(instance,service_namespace)(max_over_time(machine_registered{{{P}}}[1m])) + 0)'  # [1m]: Retire shows within a minute
SEEN = f'(max by(instance,service_namespace)({LIVE}) + 0)'
SILENT = f'label_replace(({REGISTERED} unless {SEEN}) * 0, "job", "node", "", "")'
STATUS = f'(({named(LIVE)} + 0) or {named(SILENT)})'
STATUS_SERIES = STATUS
for _label, _val in (("metric", "Status"), ("order", "0"), ("resource", "status")):
    STATUS_SERIES = f'label_replace({STATUS_SERIES}, "{_label}", "{_val}", "", "")'

# Usage cards show a value only while the machine is UP (an up == 1 sample in the last minute). Otherwise every card of a
# machine with a Status card says "No data" instead of disappearing: CPU is a rate and needs two samples in its 5 minutes
# (gone ~15 s after an agent starts and until it has run a while), while Memory / Disk are plain values that would keep
# showing the last sample for 5 minutes after the machine stopped. Every side is reduced to the same labels (`or` /
# `unless` match whole label sets in OpenObserve) and gets `+ 0` (an empty side would empty the result).
KEY = "instance,job,service_namespace"
UP_NOW = f'max by({KEY})({LIVE} == 1)'
MACHINES = f'(max by({KEY})(({LIVE} + 0) or {SILENT}) + 0)'


def usage_cards(expr, metric, order, resource):
    value = f'(max by({KEY})({expr}) * {UP_NOW})'
    nodata = f'label_replace(({MACHINES} unless ({value} + 0)) * 0, "nodata", "1", "", "")'
    return [card_series(value, metric, order, resource), card_series(nodata, metric, order, resource)]


# Amounts shown under the usage bars: CPU cores, used / total bytes of memory and disk (the same series as the percentages,
# in bytes). Each is a row of its own (resource = cores / mem_used / mem_total / disk_used / disk_total); the panel's script
# folds all rows of a machine into its one card. Not gated by UP_NOW: the script shows them only for a machine that is UP.
CORES = f'count by({KEY})(node_cpu_seconds_total{{mode="idle",{P}}})'
MEM_TOTAL = f'((node_memory_memtotal_bytes{{{P}}} + 0) or (node_memory_total_bytes{{{P}}} + 0))'
MEM_USED = (f'((node_memory_memtotal_bytes{{{P}}} - node_memory_memavailable_bytes{{{P}}})'
            f' or (node_memory_active_bytes{{{P}}} + node_memory_wired_bytes{{{P}}} + node_memory_compressed_bytes{{{P}}}))')
DISK_TOTAL = f'max by({KEY})(node_filesystem_size_bytes{{{DSEL}}})'
DISK_USED = f'({DISK_TOTAL} - max by({KEY})(node_filesystem_avail_bytes{{{DSEL}}}))'


def amount_card(expr, order, resource):
    return card_series(f'max by({KEY})({expr})', resource, order, resource)


UNION = "\n or ".join([STATUS_SERIES, *usage_cards(CPU, "CPU", 1, "cpu"), *usage_cards(MEM, "Memory", 2, "memory"),
                       *usage_cards(DISK, "Disk /", 3, "disk"),
                       amount_card(CORES, 4, "cores"), amount_card(MEM_USED, 5, "mem_used"), amount_card(MEM_TOTAL, 6, "mem_total"),
                       amount_card(DISK_USED, 7, "disk_used"), amount_card(DISK_TOTAL, 8, "disk_total")])
MACHINE_PROM = f'label_join(\n{UNION}\n, "key", "|", "instance", "resource")'

# thresholds for every machine Prometheus currently knows: own row if present, else the '*' default row
MACHINE_SQL = """SELECT i.instance || '|' || r.resource AS key,
       COALESCE(t.warn, d.warn) AS warn,
       COALESCE(t.crit, d.crit) AS crit,
       (t.instance IS NULL) AS is_default
FROM unnest(string_to_array('${instance_list:csv}', ',')) AS i(instance)
CROSS JOIN (VALUES ('cpu'), ('memory'), ('disk')) AS r(resource)
LEFT JOIN usage_threshold d ON d.instance = '*' AND d.resource = r.resource
LEFT JOIN usage_threshold t ON t.instance = i.instance AND t.resource = r.resource"""

# Services: probed by each machine's agent (agent/probes/*.yml); instance = that machine, target = the probed URL. One card
# per service with a probe result in the last 2 minutes (1 = UP, 0 = not HTTP 200), plus a DOWN card (0, label
# probe="no data", with the Retire button) for every service in the inventory (table service, exported as
# service_registered) without one: the same rule as the Service DOWN alert (gen_alerts.py SERVICE_DOWN, also for why [2m]
# and why both sides of `unless` keep the same labels).
PROBE = f'job="blackbox",{P}'
SVC_KEY = "instance,service,service_namespace"
SVC_LIVE = f'max by({SVC_KEY},target)(last_over_time(probe_success{{{PROBE}}}[2m]))'
SVC_REGISTERED = f'(max by({SVC_KEY})(max_over_time(service_registered{{{P}}}[1m])) + 0)'  # [1m]: Retire shows within a minute
SVC_SEEN = f'(max by({SVC_KEY})(last_over_time(probe_success{{{PROBE}}}[2m])) + 0)'
SVC_SILENT = f'label_replace(({SVC_REGISTERED} unless {SVC_SEEN}) * 0, "probe", "no data", "", "")'
SERVICE_Q = f'({SVC_LIVE} + 0) or {SVC_SILENT}'

# ---------------------------------------------------------------- CSS
# One card per machine (see docs/preview/fleet-card.html); a service card shares the base. Colours / the row layout are
# plain CSS so the card follows Grafana's light and dark themes (grey tints over the panel, text colour inherited).
CSS = """
.toolbar{display:flex;justify-content:flex-end;padding:0 2px 8px}
.cards{--ok:#1f9d55;--warn:#ed8129;--crit:#d93636;--accent:#5794f2;--line:rgba(128,128,128,.35);
  display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;padding:2px}
.card{position:relative;display:flex;flex-direction:column;gap:10px;padding:12px 14px 14px;min-width:0;
  border:1px solid var(--line);border-left:5px solid var(--ok);border-radius:8px;background:rgba(128,128,128,.10);color:inherit}
.card:hover{background:rgba(128,128,128,.18);border-top-color:var(--accent);border-right-color:var(--accent);border-bottom-color:var(--accent)}
.card[data-state="warn"]{border-left-color:var(--warn)}
.card[data-state="crit"],.card[data-up="0"]{border-left-color:var(--crit)}
.stretch{position:absolute;top:0;right:0;bottom:0;left:0;z-index:1;border-radius:8px}
.head{display:flex;align-items:center;gap:8px;min-width:0}
.title{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.name{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill{flex:none;padding:1px 9px;border-radius:10px;font-weight:700;font-size:11px;color:#fff;background:var(--ok)}
.card[data-up="0"] .pill{background:var(--crit)}
.btn{position:relative;z-index:2;flex:none;font:inherit;font-size:12px;line-height:1;padding:5px 9px;border-radius:6px;cursor:pointer;
  border:1px solid var(--line);background:rgba(128,128,128,.12);color:inherit}
.btn:hover{background:rgba(128,128,128,.3)}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.btn.retire{border-color:var(--crit);color:var(--crit)}
.chips{position:relative;z-index:2;display:flex;gap:4px;flex-wrap:nowrap;overflow:hidden;min-width:0}
.chips.open{flex-wrap:wrap}
.labels{display:flex;flex-wrap:wrap;gap:4px}
.chip{flex:none;max-width:100%;font-size:11px;line-height:16px;padding:0 7px;border-radius:9px;background:rgba(128,128,128,.22);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip b{font-weight:600;opacity:.7}
.chip.proj{background:rgba(87,148,242,.18);color:var(--accent);max-width:60%}
.chip.more{cursor:pointer;border:0;font:inherit;font-size:11px;color:inherit;font-weight:600}
.chip.more:hover{background:rgba(128,128,128,.4)}
.chip[data-k="Time"],.chip[data-k="Value"],.chip[data-k="metric"],.chip[data-k="order"],.chip[data-k="key"],
.chip[data-k="warn"],.chip[data-k="crit"],.chip[data-k="is_default"],.chip[data-k="resource"],.chip[data-k="__name__"],
.chip[data-k="name"],.chip[data-k="host_name"],.chip[data-k="nodata"],.chip[data-k="otel_scope_name"],.chip[data-k="otel_scope_version"]{display:none}
.metrics{display:flex;flex-direction:column;gap:10px}
.row{position:relative;z-index:2;display:grid;grid-template-columns:58px 1fr 54px;grid-template-rows:auto auto;
  column-gap:10px;row-gap:3px;align-items:center;cursor:default}
.lbl{font-size:12px;opacity:.62}
.bar{position:relative;height:8px;border-radius:4px;background:rgba(128,128,128,.26)}
.bar i{display:block;height:100%;max-width:100%;border-radius:4px;background:var(--ok)}
.bar .mk{position:absolute;top:-3px;bottom:-3px;width:2px;border-radius:1px;background:currentColor;opacity:.65}
.val{text-align:right;font-weight:600;font-size:15px;font-variant-numeric:tabular-nums}
.val small{font-size:11px;font-weight:500;opacity:.62;margin-left:1px}
.amt{grid-column:2 / 4;font-size:11px;opacity:.62;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row[data-state="warn"] .bar i{background:var(--warn)} .row[data-state="warn"] .val{color:var(--warn)}
.row[data-state="crit"] .bar i{background:var(--crit)} .row[data-state="crit"] .val{color:var(--crit)}
.row[data-state="nodata"] .bar{opacity:.45} .row[data-state="nodata"] .val{font-size:12px;font-weight:500;opacity:.62}
.tip{display:none;position:absolute;right:0;bottom:calc(100% + 6px);z-index:5;white-space:nowrap;font-size:12px;
  padding:5px 8px;border-radius:5px;background:rgba(0,0,0,.8);color:#fff;pointer-events:none}
.row:hover .tip{display:block}
.downnote{font-size:12px;color:var(--crit)}
.raw{display:none}
.svc .pill{display:none}
.svc[data-v="1"]{border-left-color:var(--ok)} .svc[data-v="0"]{border-left-color:var(--crit)}
.svc[data-v="1"] .pill.up{display:inline-block;background:var(--ok)}
.svc[data-v="0"] .pill.down{display:inline-block;background:var(--crit)}
.svc .btn[data-action="retire"]{display:none}
.svc[data-probe="no data"] .btn[data-action="retire"]{display:inline-block}
"""

# The panel's data is one row per machine and kind (status / cpu / memory / disk / cores / mem_* / disk_*); the template only
# writes them out as hidden rows and the script (AFTER_RENDER) builds one card per machine from them.
MACHINE_HTML = """<div class="toolbar"><button class="btn" data-action="defaults" title="Thresholds used by machines that have no values of their own">&#9881; Default thresholds</button></div>
<div class="raw">
{{#each data}}
<div class="r" data-instance="{{instance}}" data-job="{{job}}" data-resource="{{resource}}" data-name="{{name}}" data-ns="{{service_namespace}}" data-value="{{Value}}" data-warn="{{warn}}" data-crit="{{crit}}" data-default="{{is_default}}" data-nodata="{{nodata}}">
  <div class="labels">{{#each this}}<span class="chip" data-k="{{@key}}"><b>{{@key}}</b>:{{this}}</span>{{/each}}</div>
</div>
{{/each}}
</div>
<div class="cards"></div>"""

SERVICE_HTML = """<div class="cards">
{{#each data}}
<div class="card svc" data-v="{{Value}}" data-probe="{{probe}}" data-instance="{{instance}}" data-service="{{service}}">
  <div class="head"><span class="name">{{service}}</span><span class="pill up">UP</span><span class="pill down">DOWN</span><button class="btn" data-action="retire" title="Remove this service from monitoring: no more DOWN card or alert">Retire</button></div>
  <div class="labels">{{#each this}}<span class="chip" data-k="{{@key}}"><b>{{@key}}</b>:{{this}}</span>{{/each}}</div>
</div>
{{/each}}
</div>"""

# ---------------------------------------------------------------- JS (After Content Ready)
# shared by both panels: SQL against the monitoring database through Grafana, role check, label chips
COMMON_JS = r"""
const el = context.element, g = context.grafana;
const DS = {type: 'grafana-postgresql-datasource', uid: 'thresholds-db'};
const boot = window.grafanaBootData || {};
const base = (boot.settings && boot.settings.appSubUrl) || '';
const role = (boot.user && boot.user.orgRole) || '';
const canEdit = role !== 'Viewer' && role !== 'None';

// labels: drop empty ones (the table join gives every card every column, e.g. host_name exists on Memory but not on the
// aggregated CPU / Disk series, target on a service with results but not on a "no data" one); name / host_name (= the
// card title) and otel_scope_* are hidden by CSS
el.querySelectorAll('.chip').forEach(c => { if (!c.textContent.slice(c.dataset.k.length + 1).trim()) { c.remove(); } });

async function runSql(sql) {
  const resp = await fetch(base + '/api/ds/query', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({queries: [{refId: 'A', datasource: DS, rawSql: sql, format: 'table', rawQuery: true, editorMode: 'code'}]})
  });
  const body = await resp.json().catch(() => ({}));
  const r = body.results && body.results.A;
  if (!resp.ok || (r && r.error)) { throw new Error((r && r.error) || body.message || ('HTTP ' + resp.status)); }
  const fr = r && r.frames && r.frames[0];
  if (!fr || !fr.data || !fr.data.values.length) { return []; }
  const names = fr.schema.fields.map(f => f.name), n = fr.data.values[0].length, rows = [];
  for (let i = 0; i < n; i++) { const o = {}; names.forEach((nm, j) => { o[nm] = fr.data.values[j][i]; }); rows.push(o); }
  return rows;
}

const okInstance = s => /^[A-Za-z0-9_.:*\-]+$/.test(s);
const sqlText = s => "'" + String(s).replace(/'/g, "''") + "'";
"""

AFTER_RENDER = COMMON_JS + r"""
const LABEL = {cpu: 'CPU', memory: 'Memory', disk: 'Space'};
const RES = ['cpu', 'memory', 'disk'];

// ---- the panel's rows -> one model per machine
const HIDDEN = new Set(['Time', 'Value', 'metric', 'order', 'key', 'warn', 'crit', 'is_default', 'resource', '__name__', 'name',
                        'host_name', 'nodata', 'otel_scope_name', 'otel_scope_version', 'instance', 'job', 'service_namespace']);
const toNum = s => { const x = parseFloat(s); return isNaN(x) ? null : x; };
const machines = new Map();
el.querySelectorAll('.raw .r').forEach(r => {
  const d = r.dataset;
  let m = machines.get(d.instance);
  if (!m) { m = {instance: d.instance, job: d.job, name: d.name || d.instance, ns: d.ns, up: false, labels: [], rows: {}, b: {}}; machines.set(d.instance, m); }
  if (!m.ns) { m.ns = d.ns; }
  const v = toNum(d.value);
  if (d.resource === 'status') {
    m.up = v === 1;
    r.querySelectorAll('.chip').forEach(c => { if (!HIDDEN.has(c.dataset.k)) { m.labels.push([c.dataset.k, c.textContent.slice(c.dataset.k.length + 1)]); } });
  } else if (RES.includes(d.resource)) {
    m.rows[d.resource] = {v: d.nodata === '1' ? null : v, w: toNum(d.warn), k: toNum(d.crit), own: d.default === 'false'};
  } else {
    m.b[d.resource] = v;
  }
});

// ---- card
const esc = s => String(s).replace(/[&<>"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[ch]));
const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
const unitOf = b => Math.min(UNITS.length - 1, Math.max(0, Math.floor(Math.log(b) / Math.log(1024))));
const fmt = x => x >= 100 ? x.toFixed(0) : x.toFixed(1);
const amount = (u, t) => { const k = unitOf(t), d = Math.pow(1024, k); return fmt(u / d) + ' / ' + fmt(t / d) + ' ' + UNITS[k]; };   // in the total's unit
const rank = {nodata: 0, ok: 0, warn: 1, crit: 2};

function cardHtml(m) {
  let worst = 'ok';
  const rows = RES.map(r => {
    const x = m.rows[r] || {v: null, w: null, k: null, own: false}, v = x.v;
    const s = v == null ? 'nodata' : (x.k != null && v >= x.k) ? 'crit' : (x.w != null && v >= x.w) ? 'warn' : 'ok';
    if (rank[s] > rank[worst]) { worst = s; }
    const marks = [x.w, x.k].map(t => t == null ? '' : '<b class="mk" style="left:' + t + '%"></b>').join('');
    const thrText = x.w == null || x.k == null ? '' : ' · warn ≥ ' + x.w + '% · crit ≥ ' + x.k + '%' + (x.own ? ' (this machine)' : ' (default)');
    const why = !m.up ? 'machine DOWN' : r === 'cpu' ? 'fewer than two samples in 5 min (agent just started)' : 'no sample in the last minute';
    const tip = v == null ? 'No data: ' + why : LABEL[r] + ' ' + v.toFixed(1) + '%' + thrText;
    const used = r === 'memory' ? m.b.mem_used : m.b.disk_used, total = r === 'memory' ? m.b.mem_total : m.b.disk_total;
    const amt = !m.up ? '—' : r === 'cpu' ? (m.b.cores == null ? '—' : m.b.cores + ' cores') : (used == null || total == null || !(total > 0) ? '—' : amount(used, total));
    return '<div class="row" data-state="' + s + '"><span class="lbl">' + LABEL[r] + '</span>' +
      '<div class="bar"><i style="width:' + (v == null ? 0 : v) + '%"></i>' + marks + '</div>' +
      '<span class="val">' + (v == null ? 'No data' : v.toFixed(1) + '<small>%</small>') + '</span><span></span><span class="amt">' + amt + '</span>' +
      '<span class="tip">' + esc(tip) + '</span></div>';
  }).join('');
  const action = m.up ? '<button class="btn" data-action="edit" title="Edit thresholds">&#9998;</button>'
                      : '<button class="btn retire" data-action="retire" title="Remove this machine from monitoring: no more DOWN card or alert; deletes its own thresholds">Retire</button>';
  const extra = m.labels.map(([k, v]) => '<span class="chip" title="' + esc(k + '=' + v) + '"><b>' + esc(k) + '</b> ' + esc(v) + '</span>').join('');
  const href = base + '/d/node-exporter-details?orgId=1&amp;var-job=' + encodeURIComponent(m.job || 'node') + '&amp;var-node=' + encodeURIComponent(m.instance);
  return '<div class="card" data-instance="' + esc(m.instance) + '" data-up="' + (m.up ? 1 : 0) + '" data-state="' + worst + '">' +
    '<a class="stretch" href="' + href + '" title="Open ' + esc(m.name) + ' in Node Exporter Details" aria-label="Open ' + esc(m.name) + ' in Node Exporter Details"></a>' +
    '<div class="head"><div class="title"><span class="name" title="' + esc(m.name) + '">' + esc(m.name) + '</span>' +
    '<div class="chips"><span class="chip proj" title="project: ' + esc(m.ns || '') + '">' + esc(m.ns || '') + '</span>' + extra + '</div></div>' +
    '<span class="pill">' + (m.up ? 'UP' : 'DOWN') + '</span>' + action + '</div>' +
    '<div class="metrics">' + rows + '</div>' +
    (m.up ? '' : '<div class="downnote">超過 1 分鐘沒有回報</div>') + '</div>';
}
const list = [...machines.values()].sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
el.querySelector('.cards').innerHTML = list.map(cardHtml).join('');

// labels on one line: the ones that do not fit collapse into "+N" (click to show all); the project chip always stays
function fitChips(box) {
  box.classList.remove('open');
  box.querySelectorAll('.more').forEach(x => x.remove());
  const chips = [...box.querySelectorAll('.chip')];
  chips.forEach(c => { c.hidden = false; });
  if (box.scrollWidth <= box.clientWidth) { return; }
  const more = document.createElement('button'); more.className = 'chip more'; box.appendChild(more);
  const hidden = [];
  for (let k = chips.length - 1; k > 0 && box.scrollWidth > box.clientWidth; k--) {
    chips[k].hidden = true; hidden.unshift(chips[k]); more.textContent = '+' + hidden.length;
  }
  more.title = hidden.map(c => c.title).join('\n');
  more.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); box.classList.add('open'); hidden.forEach(c => { c.hidden = false; }); more.remove(); });
}
const fitAll = () => el.querySelectorAll('.cards .chips').forEach(fitChips);
fitAll();
if (el._fleetRO) { el._fleetRO.disconnect(); }
if (window.ResizeObserver) { el._fleetRO = new ResizeObserver(fitAll); el._fleetRO.observe(el); }

// ---- edit dialog: one dialog for the three thresholds of a machine
if (!document.getElementById('thr-modal-style')) {
  const st = document.createElement('style'); st.id = 'thr-modal-style';
  st.textContent = `
  .thr-back{position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;padding:16px}
  .thr-dlg{width:400px;max-width:100%;border-radius:8px;padding:20px 22px;box-shadow:0 10px 40px rgba(0,0,0,.5);border:1px solid rgba(128,128,128,.4)}
  .thr-dlg h3{margin:0 0 2px;font-size:18px;overflow-wrap:anywhere} .thr-dlg .sub{opacity:.7;font-size:12px;margin-bottom:14px}
  .thr-row{display:grid;grid-template-columns:70px 1fr 1fr;gap:10px;align-items:end;margin-bottom:10px}
  .thr-rn{font-weight:600;padding-bottom:5px;display:flex;flex-direction:column;line-height:1.2}
  .thr-src{font-size:10px;font-weight:500;opacity:.6}
  .thr-row label{font-size:12px;opacity:.8;display:flex;flex-direction:column;gap:3px}
  .thr-row input{width:100%;padding:6px 8px;border-radius:5px;border:1px solid rgba(128,128,128,.55);background:transparent;color:inherit;font-size:14px}
  .thr-note{font-size:12px;opacity:.7;margin:6px 0 12px} .thr-err{color:#d93636;font-size:13px;min-height:18px;margin-bottom:8px}
  .thr-btns{display:flex;gap:8px;justify-content:flex-end}
  .thr-btns button{padding:7px 14px;border-radius:6px;cursor:pointer;border:1px solid rgba(128,128,128,.55);background:rgba(128,128,128,.18);color:inherit;font-size:13px}
  .thr-btns button.primary{background:#3871dc;border-color:#3871dc;color:#fff} .thr-btns button[disabled]{opacity:.5;cursor:wait}
  .thr-btns .spacer{flex:1}
  @media (max-width:480px){.thr-row{grid-template-columns:1fr 1fr}.thr-rn{grid-column:1/-1;padding:0}}
  `;
  document.head.appendChild(st);
}

function openModal(o) {
  const old = document.getElementById('thr-modal'); if (old) { old.remove(); }
  const colors = (g.theme && g.theme.colors) || {};
  const back = document.createElement('div'); back.className = 'thr-back'; back.id = 'thr-modal';
  const dlg = document.createElement('div'); dlg.className = 'thr-dlg';
  dlg.style.background = (colors.background && colors.background.primary) || '#181b1f';
  dlg.style.color = (colors.text && colors.text.primary) || '#ccccdc';
  const h = document.createElement('h3'); h.textContent = o.title;
  const sub = document.createElement('div'); sub.className = 'sub'; sub.textContent = o.subtitle;
  dlg.append(h, sub);
  const inputs = {};
  o.rows.forEach(r => {
    const row = document.createElement('div'); row.className = 'thr-row';
    const nm = document.createElement('div'); nm.className = 'thr-rn'; nm.textContent = LABEL[r.resource] || r.resource;
    if (r.own !== undefined) { const s = document.createElement('span'); s.className = 'thr-src'; s.textContent = r.own ? 'this machine' : 'default'; nm.appendChild(s); }
    const mk = (txt, val) => { const l = document.createElement('label'); l.textContent = txt;
      const i = document.createElement('input'); i.type = 'number'; i.min = 0; i.max = 100; i.step = 1; i.value = val; l.appendChild(i); return [l, i]; };
    const [lw, iw] = mk('Warn ≥ %', r.warn), [lc, ic] = mk('Crit ≥ %', r.crit);
    inputs[r.resource] = {iw, ic}; row.append(nm, lw, lc); dlg.appendChild(row);
  });
  const note = document.createElement('div'); note.className = 'thr-note'; note.textContent = o.note; dlg.appendChild(note);
  const err = document.createElement('div'); err.className = 'thr-err'; dlg.appendChild(err);
  const btns = document.createElement('div'); btns.className = 'thr-btns';
  const mkb = (t, c) => { const b = document.createElement('button'); b.textContent = t; if (c) { b.className = c; } return b; };
  const bCancel = mkb('Cancel'), bSave = mkb('Save', 'primary'), bReset = o.allowReset ? mkb('Use default') : null;
  const spacer = document.createElement('span'); spacer.className = 'spacer';
  if (bReset) { btns.appendChild(bReset); } btns.append(spacer, bCancel, bSave); dlg.appendChild(btns);
  back.appendChild(dlg); document.body.appendChild(back);

  const close = () => { document.removeEventListener('keydown', onKey); back.remove(); };
  const onKey = e => { if (e.key === 'Escape') { close(); } };
  document.addEventListener('keydown', onKey);
  back.addEventListener('mousedown', e => { if (e.target === back) { close(); } });
  bCancel.addEventListener('click', close);

  const busy = on => { [bSave, bCancel, bReset].forEach(b => { if (b) { b.disabled = on; } }); };
  const run = async sql => {
    busy(true); err.textContent = '';
    try { await runSql(sql); g.notifySuccess(['Thresholds saved']); close(); g.refresh(); }
    catch (e) { err.textContent = String(e.message || e); busy(false); }
  };
  bSave.addEventListener('click', () => {
    if (!okInstance(o.instance)) { err.textContent = 'Invalid instance name'; return; }
    const vals = [];
    for (const r of o.rows) {
      const w = Number(inputs[r.resource].iw.value), k = Number(inputs[r.resource].ic.value);
      if (!Number.isInteger(w) || !Number.isInteger(k) || w < 0 || k > 100) { err.textContent = 'Use whole numbers between 0 and 100'; return; }
      if (w >= k) { err.textContent = (LABEL[r.resource] || r.resource) + ': warn must be lower than crit'; return; }
      vals.push("('" + o.instance + "','" + r.resource + "'," + w + "," + k + ")");
    }
    run('INSERT INTO usage_threshold (instance, resource, warn, crit) VALUES ' + vals.join(',') +
        ' ON CONFLICT (instance, resource) DO UPDATE SET warn = EXCLUDED.warn, crit = EXCLUDED.crit, updated_at = now() RETURNING instance');
  });
  if (bReset) {
    bReset.addEventListener('click', () => {
      const rs = o.rows.map(r => "'" + r.resource + "'").join(',');
      run("DELETE FROM usage_threshold WHERE instance = '" + o.instance + "' AND instance <> '*' AND resource IN (" + rs + ') RETURNING instance');
    });
  }
}

el.querySelectorAll('button[data-action]').forEach(b => {
  if (!canEdit) { b.disabled = true; b.title = 'Editing thresholds requires the Editor or Admin role'; return; }
  b.addEventListener('click', async ev => {
    ev.preventDefault(); ev.stopPropagation();
    try {
      if (b.dataset.action === 'retire') {
        const c = b.closest('.card'), inst = c.dataset.instance, m = machines.get(inst);
        if (!okInstance(inst) || inst === '*') { g.notifyError(['Invalid instance name']); return; }
        if (!window.confirm('Retire ' + m.name + '?\n\nIt is removed from monitoring (no more DOWN card or alert) and its own thresholds are deleted. ' +
                            'If it sends data again it comes back as a new machine with the default thresholds.')) { return; }
        await runSql("WITH t AS (DELETE FROM usage_threshold WHERE instance = '" + inst + "' AND instance <> '*') " +
                     "DELETE FROM machine WHERE instance = '" + inst + "' RETURNING instance");
        g.notifySuccess([m.name + ' retired']); g.refresh();
      } else if (b.dataset.action === 'edit') {
        const m = machines.get(b.closest('.card').dataset.instance);
        const rows = RES.filter(r => m.rows[r] && m.rows[r].w != null && m.rows[r].k != null)
                        .map(r => ({resource: r, warn: m.rows[r].w, crit: m.rows[r].k, own: m.rows[r].own}));
        const own = rows.some(r => r.own);
        openModal({
          title: m.name, subtitle: (m.ns || '') + ' · thresholds', instance: m.instance, allowReset: own, rows,
          note: own ? 'Saving stores all three for this machine. "Use default" removes this machine\'s values.'
                    : 'This machine uses the default values. Saving creates values for this machine only.'
        });
      } else {
        const rows = await runSql("SELECT resource, warn, crit FROM usage_threshold WHERE instance = '*' ORDER BY CASE resource WHEN 'cpu' THEN 1 WHEN 'memory' THEN 2 ELSE 3 END");
        openModal({title: 'Default thresholds', subtitle: 'Used by every machine that has no values of its own', instance: '*', allowReset: false, rows,
                   note: 'Machines with their own values are not affected.'});
      }
    } catch (e) { g.notifyError([String(e.message || e)]); }
  });
});
"""

SERVICE_AFTER = COMMON_JS + r"""
el.querySelectorAll('button[data-action="retire"]').forEach(b => {
  if (!canEdit) { b.disabled = true; b.title = 'Retiring a service requires the Editor or Admin role'; return; }
  b.addEventListener('click', async ev => {
    ev.preventDefault(); ev.stopPropagation();
    const c = b.closest('.card'), inst = c.dataset.instance, svc = c.dataset.service;
    if (!okInstance(inst) || !svc) { g.notifyError(['Invalid instance or service name']); return; }
    if (!window.confirm('Retire ' + svc + ' on ' + inst + '?\n\nIt is removed from monitoring (no more DOWN card or alert). ' +
                        'If its probe results arrive again it comes back.')) { return; }
    try {
      await runSql("DELETE FROM service WHERE instance = " + sqlText(inst) + " AND service = " + sqlText(svc) + " RETURNING service");
      g.notifySuccess([svc + ' retired']); g.refresh();
    } catch (e) { g.notifyError([String(e.message || e)]); }
  });
});
"""


def text_panel(pid, title, y, h, targets, html, empty, transformations, after=""):
    return {
        "id": pid, "type": PLUGIN, "title": title, "datasource": MIXED, "transparent": True,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": h},
        "targets": targets, "transformations": transformations,
        "options": {"renderMode": "allRows", "content": html, "defaultContent": empty,
                    "editor": {"format": "auto", "language": "html"}, "editors": [],
                    "externalStyles": [], "contentPartials": [], "helpers": "", "status": "",
                    "afterRender": after, "styles": CSS, "wrap": True},
    }


def prom_t(expr, ref="A"):
    return {"datasource": PROM, "expr": expr, "refId": ref, "instant": True, "range": False, "format": "table"}


dash = {
    "uid": "fleet-overview", "title": "Fleet Overview", "tags": ["overview"],
    "timezone": "browser", "schemaVersion": 39, "version": 8, "editable": True,
    "refresh": "30s", "time": {"from": "now-6h", "to": "now"},
    "links": o2_links.links(),
    "panels": [
        text_panel(1, "Machines", 0, 15,
                   [prom_t(MACHINE_PROM, "A"),
                    {"datasource": PG, "refId": "B", "format": "table", "rawQuery": True,
                     "editorMode": "code", "rawSql": MACHINE_SQL}],
                   MACHINE_HTML, "No machines match the selected namespace.",
                   [{"id": "joinByField", "options": {"byField": "key", "mode": "outer"}},
                    # drop threshold rows that have no matching metric (e.g. a machine that is down)
                    {"id": "filterByValue", "options": {"type": "include", "match": "all",
                     "filters": [{"fieldName": "resource", "config": {"id": "isNotNull"}}]}},
                    {"id": "sortBy", "options": {"sort": [{"field": "name"}, {"field": "order"}]}}],
                   AFTER_RENDER),
        text_panel(2, "Services", 15, 5, [prom_t(SERVICE_Q)], SERVICE_HTML,
                   "No probed services match the selected namespace (agent/probes/*.yml on each machine).",
                   [{"id": "sortBy", "options": {"sort": [{"field": "service"}, {"field": "instance"}]}}],
                   SERVICE_AFTER),
    ],
    "templating": {"list": [
        {"name": "ds", "label": "Datasource", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "refresh": 1},
        {"name": "namespace", "label": "Namespace", "type": "query", "datasource": PROM,
         "query": {"query": 'label_values(up, service_namespace)', "refId": "namespace-var"},
         "definition": 'label_values(up, service_namespace)', "multi": True, "includeAll": True,
         "allValue": ".*", "current": {"selected": True, "text": "All", "value": "$__all"},
         "refresh": 2, "sort": 1},
        {"name": "instance_list", "label": "Machines", "type": "query", "datasource": PROM, "hide": 2,
         "query": {"query": 'label_values(up{job="node",service_namespace=~"$namespace"}, instance)', "refId": "inst-var"},
         "definition": 'label_values(up{job="node",service_namespace=~"$namespace"}, instance)',
         "multi": True, "includeAll": True, "current": {"selected": True, "text": "All", "value": "$__all"},
         "refresh": 2, "sort": 1},
    ]},
}
print(json.dumps(dash, indent=2))
