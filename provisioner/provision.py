"""Generate every dashboard and alert rule from what exists right now; nothing generated is kept in the repo.

Runs as the `provisioner` service, every SYNC_INTERVAL seconds:
  0. OpenObserve: keep the two routing pipelines in place (traces / logs from stream "ingest" -> the stream named after the
     record's service.namespace, i.e. one stream per project, created on its first record), and set each project stream's
     retention from retention.json (unlisted projects: OpenObserve's default, O2_RETENTION_DAYS)
     and register every machine that is sending its status in the machine inventory (monitoring database, table machine):
     a registered machine that goes quiet is DOWN (alert, red card) until it is retired from its Fleet Overview card
  1. ask OpenObserve which traces streams exist (one per project)
  2. Grafana: write the provisioning tree into the shared volume (OUT_DIR, mounted read-only by Grafana at
     /etc/grafana/provisioning): the static files from grafana/provisioning/ plus the generated Fleet Overview,
     Node Exporter Details (with OpenObserve links per stream) and alert rules. Grafana re-reads dashboard files
     every 30 s; datasources and alert rules only on its start, which waits for the first pass (OUT_DIR/.ready).
  3. OpenObserve: one "API Service" dashboard per traces stream. Created when the stream appears, deleted when it
     is gone (retention or deletion), and overwritten when someone edits it in the UI.
Machines need nothing here: Grafana's dashboards and rules find them through PromQL at query time.
Only the standard library is used, so it runs in a bare python image.
"""
import base64
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import openobserve_dashboards

HERE = pathlib.Path(__file__).resolve().parent
GRAFANA_GEN = HERE / "grafana"
STATIC_DIR = pathlib.Path(os.environ.get("STATIC_DIR", "/static"))  # grafana/provisioning/ from the repo
OUT_DIR = pathlib.Path(os.environ.get("OUT_DIR", "/out"))
RETENTION_FILE = pathlib.Path(os.environ.get("RETENTION_FILE", "/retention.json"))
FOLDER = "default"
MARKER = "[managed: api-service/{}]"
# one value per process: gen_alerts.py puts it on the heartbeat so each start sends a startup digest (see there)
os.environ.setdefault("PROVISIONER_STARTED", datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
                      .strftime("%Y-%m-%d %H:%M"))
INGEST = "ingest"  # the collector writes every trace / log here; the pipelines move them out (otel-collector/config.yaml)
# where the namespace ends up in each record: OpenObserve prefixes resource attributes with service_ in traces, not in logs
NAMESPACE_FIELD = {"traces": "service_service_namespace", "logs": "service_namespace"}


def log(msg):
    print(msg, flush=True)


class O2:
    def __init__(self, url, org, user, password):
        self.url = url.rstrip("/")
        self.base = f"{self.url}/api/{org}"
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": self.auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"{method} {path} -> {err.code}: {err.read().decode(errors='replace')[:500]}") from None

    def promql(self, query):
        return self.call("GET", "/prometheus/api/v1/query?" + urllib.parse.urlencode({"query": query}))["data"]["result"]

    def streams(self, stream_type):
        """name -> list item (with settings), without the ingest stream."""
        return {s["name"]: s for s in self.call("GET", f"/streams?type={stream_type}")["list"] if s["name"] != INGEST}

    def top_service(self, stream):
        """Most frequent service_name in the stream over the last 7 days: the Service dropdown's default."""
        now = int(time.time() * 1e6)
        sql = f'SELECT service_name, COUNT(*) AS n FROM "{stream}" GROUP BY service_name ORDER BY n DESC LIMIT 1'
        hits = self.call("POST", "/_search?type=traces",
                         {"query": {"sql": sql, "start_time": now - 7 * 86400 * 10**6, "end_time": now}})["hits"]
        return hits[0]["service_name"] if hits else ""

    def managed(self):
        """stream -> dashboard list item, for dashboards carrying our marker."""
        prefix = MARKER.format("")[:-1]
        found = {}
        for item in self.call("GET", f"/dashboards?folder={FOLDER}")["dashboards"]:
            desc = item.get("description") or ""
            start = desc.rfind(prefix)
            if start != -1 and desc.endswith("]"):
                found[desc[start + len(prefix):-1]] = item
        return found


# ----------------------------------------------------------------------------------------- OpenObserve routing
def wanted_pipeline(stream_type):
    def node(node_id, stream, io_type, x):
        return {"id": node_id, "position": {"x": x, "y": 0}, "io_type": io_type,
                "data": {"node_type": "stream", "org_id": "default", "stream_name": stream, "stream_type": stream_type}}
    return {
        "name": f"route_{stream_type}_by_namespace", "enabled": True,
        "description": "Moves every record from stream ingest to the stream of its service.namespace. "
                       "Managed by the observability provisioner: changes are reverted.",
        "source": {"source_type": "realtime", "org_id": "default", "stream_name": INGEST, "stream_type": stream_type},
        "nodes": [node("ingest", INGEST, "input", 0), node("project", "{%s}" % NAMESPACE_FIELD[stream_type], "output", 300)],
        "edges": [{"id": "eingest-project", "source": "ingest", "target": "project"}],
    }


def _shape(p):
    """what matters for comparing a pipeline from the API with the wanted one"""
    return (p.get("enabled"), json.dumps(p.get("source"), sort_keys=True),
            sorted((n["io_type"], json.dumps(n["data"], sort_keys=True)) for n in p.get("nodes", [])),
            sorted((e["source"], e["target"]) for e in p.get("edges", [])))


def ensure_pipelines(o2):
    existing = {p["name"]: p for p in o2.call("GET", "/pipelines")["list"]}
    for stream_type in NAMESPACE_FIELD:
        want = wanted_pipeline(stream_type)
        have = existing.get(want["name"])
        if have and _shape(have) == _shape(want):
            continue
        if have:
            o2.call("DELETE", f"/pipelines/{have['pipeline_id']}")
        o2.call("POST", "/pipelines", want)
        log(f"o2       {'re' if have else ''}created pipeline {want['name']}")


def apply_retention(o2, streams_by_type):
    try:
        days = {k: int(v) for k, v in json.loads(RETENTION_FILE.read_text()).items() if not k.startswith("_")}
    except (OSError, ValueError) as exc:
        log(f"o2       cannot read {RETENTION_FILE} ({exc}); retention left as it is")
        return
    for stream_type, streams in streams_by_type.items():
        for name, item in streams.items():
            want = days.get(name, 0)  # 0 = OpenObserve's default (ZO_COMPACT_DATA_RETENTION_DAYS)
            if (item.get("settings") or {}).get("data_retention", 0) != want:
                o2.call("PUT", f"/streams/{name}/settings?type={stream_type}", {"data_retention": want})
                log(f"o2       retention {stream_type}/{name} = {want or 'default'} days")


# ----------------------------------------------------------------------------------------- machine inventory
def sql_text(value):
    return "'" + str(value).replace("'", "''") + "'"


def register_machines(o2):
    """Upsert every machine with a current status sample (its agent is sending) into table machine."""
    seen = {r["metric"].get("instance"): r["metric"].get("service_namespace", "")
            for r in o2.promql('max by(instance, service_namespace)(up{job="node"})')}
    seen.pop(None, None)
    if not seen:
        return
    rows = ", ".join(f"({sql_text(i)}, {sql_text(ns)})" for i, ns in sorted(seen.items()))
    sql = (f"INSERT INTO machine (instance, service_namespace) VALUES {rows} "
           "ON CONFLICT (instance) DO UPDATE SET service_namespace = EXCLUDED.service_namespace, last_seen = now() "
           "RETURNING (xmax = 0) AS added, instance")
    env = dict(os.environ, PGHOST=os.environ["PG_HOST"], PGPORT=os.environ.get("PG_PORT", "5432"),
               PGUSER=os.environ["PG_USER"], PGPASSWORD=os.environ["PG_PASSWORD"],
               PGSSLMODE=os.environ.get("PG_SSLMODE", "require"), PGDATABASE="monitoring")
    done = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          env=env, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[-300:])
    for line in done.stdout.split():
        added, _, instance = line.partition("|")
        if added == "t":
            log(f"machine  registered {instance}")


# ----------------------------------------------------------------------------------------- Grafana
def run_generator(script, *args, streams):
    env = dict(os.environ, O2_STREAMS=",".join(streams))
    done = subprocess.run([sys.executable, str(GRAFANA_GEN / script), *args], env=env, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"{script}: {done.stderr.strip()[-500:]}")
    return done.stdout


def write_if_changed(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == text:
        return False
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)  # atomic: Grafana never reads a half-written file
    return True


def provision_grafana(streams):
    files = {p.relative_to(STATIC_DIR): p.read_text() for p in STATIC_DIR.rglob("*") if p.is_file()}
    files[pathlib.Path("dashboards/fleet-overview.json")] = run_generator("gen_fleet_overview.py", streams=streams)
    files[pathlib.Path("dashboards/node-exporter-details.json")] = run_generator("gen_node_details.py", streams=streams)
    files[pathlib.Path("alerting/monitoring-alerts.yml")] = run_generator(
        "gen_alerts.py", os.environ.get("ALERTS_MODE", "prod"), streams=streams)

    for rel, text in files.items():
        if write_if_changed(OUT_DIR / rel, text):
            log(f"grafana  wrote {rel}")
    for path in OUT_DIR.rglob("*"):  # files no longer generated (e.g. a static file removed from the repo)
        rel = path.relative_to(OUT_DIR)
        if path.is_file() and rel not in files and rel.name != ".ready":
            path.unlink()
            log(f"grafana  removed {rel}")
    (OUT_DIR / ".ready").touch()


# ----------------------------------------------------------------------------------------- OpenObserve
def sync_openobserve(o2, streams, state):
    """state: stream -> (body digest, server hash) as last written by this process."""
    wanted = {}
    for stream in streams:
        body = openobserve_dashboards.api_service(stream, o2.top_service(stream))
        body["description"] = f"{body['description']}\n\n{MARKER.format(stream)}"
        wanted[stream] = (body, json.dumps(body, sort_keys=True))
    server = o2.managed()
    changes = 0

    for stream, (body, digest) in wanted.items():
        item = server.get(stream)
        if item is None:
            o2.call("POST", f"/dashboards?folder={FOLDER}", body)
            log(f"o2       created api-service/{stream}")
        elif state.get(stream) != (digest, item["hash"]):
            if stream not in state:
                reason = "first pass"
            elif state[stream][0] != digest:
                reason = "generator changed"
            else:
                reason = "edited in the UI, reverting"
            o2.call("PUT", f"/dashboards/{item['dashboard_id']}?folder={FOLDER}&hash={item['hash']}",
                    dict(body, dashboardId=item["dashboard_id"]))
            log(f"o2       updated api-service/{stream} ({reason})")
        else:
            continue
        changes += 1

    for stream, item in server.items():
        if stream not in wanted:
            o2.call("DELETE", f"/dashboards/{item['dashboard_id']}?folder={FOLDER}")
            state.pop(stream, None)
            log(f"o2       deleted api-service/{stream} (stream no longer exists)")
            changes += 1

    if changes:  # remember the hashes the server gave our writes, to recognise later UI edits
        server = o2.managed()
    for stream, (_, digest) in wanted.items():
        if stream in server:
            state[stream] = (digest, server[stream]["hash"])


def main():
    o2 = O2(os.environ.get("O2_URL", "http://openobserve:5080/openobserve"), os.environ.get("O2_ORG", "default"),
            os.environ["ZO_ROOT_USER_EMAIL"], os.environ["ZO_ROOT_USER_PASSWORD"])
    interval = int(os.environ.get("SYNC_INTERVAL", "60"))
    streams, state = None, {}
    (OUT_DIR / ".ready").unlink(missing_ok=True)  # healthy again after this start's first pass, not a previous one
    log(f"provisioning every {interval}s")
    while True:
        try:
            register_machines(o2)
        except Exception as exc:
            log(f"machine  registration failed: {exc}")
        try:
            ensure_pipelines(o2)
            by_type = {t: o2.streams(t) for t in NAMESPACE_FIELD}
            apply_retention(o2, by_type)
            streams = sorted(by_type["traces"])
        except Exception as exc:  # OpenObserve starting or restarting: keep the last known list
            log(f"o2       not reachable or failed ({exc}); streams: {streams if streams is not None else 'none yet'}")
        try:
            provision_grafana(streams or [])
        except Exception as exc:
            log(f"grafana  failed: {exc}")
        if streams is not None:
            try:
                sync_openobserve(o2, streams, state)
            except Exception as exc:
                log(f"o2       sync failed: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
