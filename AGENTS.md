# Observability stack — read this first (written for AI agents)

OpenObserve + Grafana + OpenTelemetry Collector in Docker Compose (this folder), node_exporter as a Homebrew service on the Mac.
Not deployed yet, but built to be: all state is in a bucket + one Postgres server chosen by env (see "Storage"); no remote machine
is monitored yet. The old Prometheus + Grafana stack on the GCP VM (`ssh GCP_aoeskills`, `~/monitoring`, its own AGENTS.md) is a
reference only — see `docs/takeaways-from-grafana.md`. The owner writes Chinese (Traditional); answer in the language they use.

## Rules
- **Generated content is not in the repo.** Dashboards and alert rules are built at runtime by the `provisioner` service
  (`provisioner/provision.py`) from what exists right now: one OpenObserve "API Service" dashboard per traces stream, Grafana
  links per stream. Change the generators in `provisioner/`, never the UI (UI edits are reverted / cannot be saved), and never
  commit generated JSON. Machines need no generation: Grafana's dashboards and rules find them with PromQL at query time.
- Services listen on **127.0.0.1 only**. `.env` holds the credentials (gitignored, chmod 600); never print or commit it.
- Only the gateway (`otel-collector`) holds OpenObserve credentials. Apps send OTLP to their machine's agent (localhost, no
  auth); agents authenticate to the gateway with a client certificate from our own CA (mTLS). See "Agent + gateway".
- **No state in container volumes.** OpenObserve data → S3-API bucket; OpenObserve metadata, Grafana's database and the thresholds
  → one Postgres server (three databases). The only volumes are OpenObserve's WAL, Grafana's plugin cache and the generated
  provisioning files. Start with `docker compose -f docker-compose.local.yml|docker-compose.prod.yml ...`; never put storage
  settings in `.env` (a value there overrides the included `.local.env` / `.prod.env`).
- Services decide their own request limits; this stack only has to survive whatever they send. The collector
  enforces that (see "Ingest limits"); keep those limits when changing the pipeline.

## Layout
| path | what |
|---|---|
| `docker-compose.yml` | the observer's services; never started alone. Bucket + Postgres settings from env (`O2_S3_*`, `PG_*`, `O2_EXT_AUTH_SALT`), refuses to start if one is missing |
| `docker-compose.local.yml` | local mode, one project: includes `docker-compose.yml` + `docker-compose.local-storage.yml` (env `.local.env`) and this Mac's agent (`agent/`, env `agent/.env`) |
| `docker-compose.local-storage.yml` | local only: bucket (RustFS, console 127.0.0.1:9001), bucket-init (creates the bucket), postgres |
| `docker-compose.prod.yml` | prod mode (observer host): includes `docker-compose.yml` with env `.prod.env` (copy `.prod.env.example`, gitignored) |
| `.env` / `.local.env` / `.prod.env` | shared secrets (root, Grafana, LINE) / local storage settings (dev-only, committed) / prod storage settings |
| `db/init.sql` | run by `db-init` on every start: creates databases `openobserve`, `grafana`, `monitoring` and table `usage_threshold` if missing |
| `otel-collector/config.yaml` | the gateway: OTLP gRPC + mTLS from agents → OpenObserve stream `ingest` (pipelines move it to one stream per `service.namespace`); scrapes postgres-exporter (thresholds, machine inventory) → Prometheus remote write → OpenObserve |
| `retention.json` | days OpenObserve keeps each project's traces / logs (`sclaser_food`: 90); unlisted projects use `O2_RETENTION_DAYS` |
| `agent/` | the per-machine agent (compose + config + `.env.example`; `docker-compose.mac.yml` for this Mac): apps' OTLP + local node_exporter → gateway |
| `scripts/pki.sh` | private CA, gateway certificate, one client certificate per machine → `pki/` (gitignored) |
| `provisioner/provision.py` | the runtime loop (every `DASHBOARD_SYNC_INTERVAL` s); stdlib only |
| `provisioner/openobserve_dashboards.py` | OpenObserve "API Service" dashboard for one stream (SQL over traces / logs) |
| `provisioner/grafana/` | Grafana generators ported from the GCP stack (`gen_fleet_overview.py`, `gen_node_details.py` + stock `node-exporter-full-1860.json`, `gen_alerts.py`), `o2_links.py` |
| `grafana/provisioning/` | the static part of Grafana's provisioning (datasources, dashboard provider); the provisioner copies it next to the generated files |
| `postgres-exporter/queries.yaml` | thresholds + machine inventory → metrics |
| `node-exporter/` | flags for this Mac's brew service (`scripts/setup-node-exporter-macos.sh`); Linux VMs run node_exporter from `agent/` |
| `docs/agent-runbook.md` | adding a new VM (certificate, `agent/.env`, apps, checks, troubleshooting, rename / remove) |
| `tools/fake_line_server.py` | stand-in for the LINE push API, for alert tests |

## Data model (what queries can rely on)
- **Metrics**: Prometheus names and labels are kept (remote write), so PromQL from the old stack works. Every metric name is
  its own OpenObserve stream, which is why the collector drops metrics nobody uses (exporters' `go_*`, postgres-exporter's
  `pg_*`). Labels: `job`, `instance`, `service_namespace` (the project, see "Projects"). For machines `instance` is the
  machine name = OpenTelemetry `host.name`, detected by the agent (resourcedetection, re-run every minute: `HOST_NAME` in
  `agent/.env` if set > GCP instance name > OS hostname), also in label `host_name`; traces / logs carry the same
  `host.name` (OpenObserve `service_host_name` / `host_name`). Only the certificate's name stays fixed. Owner's requirement
  (kept from the GCP stack): a VM renamed at the provider must show its new name — unverified until the first GCP deploy. Fleet Overview shows `instance` as the card name, read from
  the same series as the value: a rename shows with the next sample, the old card is gone within ~5 min (verified
  2026-10-13). The GCP-era `provider_instance_name_info` textfile is gone: on GCP the card name was joined from
  a separate name metric (fallback: OS hostname), which is why changing `instance` there did not change the card.
  macOS has no `node_memory_MemAvailable_bytes`; memory panels use `... or (active + wired + compressed) / total`.
  On macOS `mountpoint="/"` shows the whole APFS container.
- **Traces** (stream per project = `service.namespace`, e.g. `sclaser_food`; see "Projects"): `duration` is in **microseconds**. One request = root span from `@opentelemetry/instrumentation-http`
  (named just `GET`/`POST`) + Next.js child spans. Every span of a logged-in request carries `enduser_id` (sclaser-food DB id)
  and `user_name` (username = phone number). Per-route numbers: filter
  `next_span_type = 'BaseServer.handleRequest' AND http_route IS NOT NULL` (one per request, with `http_route`, `http_status_code`).
- **Logs** (same project stream): from pino via `@opentelemetry/instrumentation-pino`; `severity`, `body`, `trace_id`, `span_id`,
  `enduser_id` / `user_name` inside requests.
  Nested objects are flattened into one column per key (`request_headers_*`); that is why request bodies are logged as one
  JSON string (`request_body`) — arbitrary bodies would keep adding columns (limit 1000 per record).

## Storage
- Write path: ingest → WAL on the container's disk (volume `openobserve-wal`) → after `ZO_MAX_FILE_RETENTION_TIME` (600 s) a WAL file
  becomes parquet on the same disk → about 10 min later uploaded to the bucket (`files/<org>/<type>/<stream>/YYYY/MM/DD/HH/`) +
  row in Postgres `file_list`. Observed 2026-10-12: first data 13:42, parquet 13:52, in the bucket ~14:02.
- Queries merge both: `file_list` → parquet from the bucket, plus the WAL / memory for the newest data. Dashboards are real time;
  a query over the last hour returns the full hour (verified: one SQL over 13:42–14:23 had every minute, the last one only in the WAL).
- The compactor merges small files and applies retention in the bucket — never add bucket lifecycle rules (`file_list` would point to deleted files).
- Losing the container disk loses only the not-yet-uploaded WAL (≤ ~20 min). Losing Postgres orphans the bucket (data present, unlisted).
  Verified 2026-10-12: a fresh container with an empty disk, same bucket + DSN, served the old logs, metrics and dashboards.
- One Postgres server, three databases (`db/init.sql`): `openobserve` (metadata: schemas, file list, users, dashboards),
  `grafana` (Grafana's own DB: users, alert state), `monitoring` (thresholds). Separate databases because OpenObserve and
  Grafana each run their own migrations. `db-init` creates them when the user has CREATEDB; otherwise create them by hand.
  `PG_PASSWORD` goes into OpenObserve's DSN URL, so it must be URL-safe.
- One S3 client for both modes (`ZO_S3_PROVIDER=s3`): RustFS locally, GCS through its S3 XML API with an HMAC key in prod
  (`ZO_S3_PROVIDER=gcs` would switch to the native GCS client, which ignores the endpoint, so it cannot be emulated locally).
  The GCS path is not yet tested against a real bucket. OpenObserve never creates the bucket (local: `bucket-init`; prod: create it in GCP).
- `ZO_EXT_AUTH_SALT` (16–48 bytes) is required on a new install since v1.1.0 (crash-loop with "db init failed" otherwise) and must stay
  the same for the lifetime of the Postgres DB.
- MinIO is not used: its community edition stopped publishing images in 2025-10 and the repo is archived.

## Projects = `service.namespace` (owner's decision 2026-10-13)
One name end to end: OpenTelemetry's resource attribute **`service.namespace`** is the project. Everything else derives from it:
| where | name |
|---|---|
| app (OTel SDK resource) / agent | `service.namespace` (sclaser-food sets `sclaser_food` in `src/otel-preload.ts`); agent env `SERVICE_NAMESPACE` |
| metrics (Prometheus label) | `service_namespace`; Grafana variable `namespace` (Fleet Overview filter), alert texts |
| OpenObserve traces / logs field | `service_service_namespace` (traces prefix resource attributes with `service_`) / `service_namespace` |
| OpenObserve stream | the namespace itself, created automatically on its first record |
- Flow: the agent fills `service.namespace` with its machine's `SERVICE_NAMESPACE` when an app sets none (`action: insert`, the
  app's own value wins). The gateway sets `default` if still missing and replaces characters outside `[A-Za-z0-9_]` with `_`;
  OpenObserve lowercases stream names (`My-Proj` → stream `my_proj`). The gateway writes every trace / log to stream `ingest`;
  two OpenObserve real-time pipelines (`route_traces_by_namespace`, `route_logs_by_namespace`, kept in place by the
  provisioner) send each record to stream `{service_service_namespace}` / `{service_namespace}` (dynamic destination name).
  `ingest` itself stays empty (the pipelines have no destination back to it).
- A record whose namespace field is missing is **dropped** by the pipeline ("Field name ... not found in record") — that is why the
  gateway always sets one. A new project therefore needs nothing on the observer: send data, the stream, its dashboard,
  the Grafana links appear; add a line to `retention.json` if it should not keep the default 30 days.
- Retention: per stream `data_retention` (0 = `O2_RETENTION_DAYS`), set by the provisioner from `retention.json`; OpenObserve's
  compactor deletes expired files from the bucket (never bucket lifecycle rules). Setting it is verified; actual deletion
  after the period is not yet observed.
- Verified 2026-10-13 through agent → gateway → OpenObserve: namespace `sclaser_food` → stream `sclaser_food`, none → the agent's
  `local`, `My-Proj` → `my_proj`; trace list API works on the routed streams; logs route the same way.

## Provisioner (runtime generation)
- Every pass: keep the two routing pipelines as wanted (missing / changed → recreated), set retention from `retention.json`,
  list traces streams in OpenObserve → write Grafana provisioning into volume `grafana-provisioning` (static files from
  `grafana/provisioning/` + Fleet Overview, Node Exporter Details, alert rules) → create / update / delete OpenObserve dashboards
  (marker `[managed: api-service/<stream>]` in the description; dashboards without it are left alone). The Service dropdown
  defaults to the stream's most frequent `service_name` over 7 days.
- Grafana re-reads dashboard files every 30 s; datasources and alert rules only on start. Grafana starts after the provisioner's
  first pass (healthcheck on `/out/.ready`, reset on each provisioner start). After changing `gen_alerts.py`:
  `docker compose -f docker-compose.<mode>.yml restart provisioner grafana` (provisioner first; it rewrites the rules file at start).
- Verified 2026-10-12: a new stream got its OpenObserve dashboard and Grafana links within one pass; deleting the stream removed
  both; an API edit of the dashboard was reverted on the next pass.

## Grafana: machines, services, thresholds, alerts
Split decided by the owner (2026-10-12): Grafana for machine status, thresholds and LINE alerts (the GCP design, ported);
OpenObserve for storage and logs/traces. Grafana has **no Prometheus of its own**: its datasource `openobserve` is
OpenObserve's Prometheus-compatible API (`/api/default/prometheus`, basic auth from `.env`).
- Generators in `provisioner/grafana/` are the GCP ones with these changes: `round(x, 0.1)` → `round(x * 10) / 10` (OpenObserve
  rejects the 2-arg form), macOS memory branch (`or (active + wired + compressed) / total`), Grafana served at `/grafana` behind Caddy as on GCP
  (`caddy/Caddyfile`, `GF_SERVER_SERVE_FROM_SUB_PATH`), datasource uid `openobserve`, machine-down pending 4m (owner: "every minute, 5 in a row").
- OpenObserve's Prometheus API: `label/<name>/values` needs `match[]` (Grafana sends it when the variable query names a
  metric), no `status/buildinfo` (harmless). Tested: label_replace, label_join, group_left, `^ 0`, last_over_time, absent, unless
  (see the label_replace / set-operation quirks under "OpenObserve gotchas").
- A label change on a scrape target leaves the old series "current" for ~5 min (remote write has no staleness markers), so
  joins on `instance` (`group_left(name)`) fail with "duplicate series" for 5 min and the alert rules error (execErrState →
  one alert message without labels). Avoid renaming labels on live targets.
- **Machine inventory** (owner's decision 2026-10-13, the usual push-model "loss of signal" design): agents push, so a dead
  machine is silence, not `up = 0`. The provisioner upserts every machine with a current `up` into `monitoring.machine`
  (exported as `machine_registered{instance,service_namespace}`). Machine DOWN = registered without an `up == 1` sample in
  the last minute, pending 4m; Fleet Overview shows a red DOWN card with a **Retire** button (deletes the machine row and
  its own thresholds; a machine that sends again is re-registered). Both read `machine_registered` over `[1m]` so a retire
  shows within a minute. Verified 2026-10-13: agent stopped → DOWN card in 1 min, alert firing after ~6 min; Retire via
  the button's SQL → card + alert gone in 1 min; agent started → registered again, UP.
- Thresholds: database `monitoring`, table `usage_threshold`, edited from the Fleet Overview cards (datasource uid `thresholds-db`);
  postgres-exporter turns it into `usage_threshold_warn/crit{instance,resource}` (collector `honor_labels`) for the alert rules (Grafana cannot use a
  Prometheus-vs-Postgres expression as an alert condition — GCP finding).
- LINE credentials: container env from `.env`, read on **recreate** (`docker compose -f docker-compose.<mode>.yml up -d grafana`), not on restart.
  Tested on 13.2.3 with tools/fake_line_server.py (token fingerprints): a new non-empty token replaces the stored one, an empty
  value keeps the old one; fresh DB + empty → no header, then filled + recreate → new token used.
- Test without LINE: run `fake-line` (see `tools/fake_line_server.py`), then
  `LINE_PUSH_URL=http://fake-line:8080/v2/bot/message/push docker compose -f docker-compose.local.yml up -d --wait provisioner` and
  `docker compose -f docker-compose.local.yml restart grafana`; afterwards the same without `LINE_PUSH_URL`. `ALERTS_MODE=test` instead gives
  1-minute timers and a fake token. Verified 2026-10-12 (before the move to runtime generation): the (since removed) Service DOWN
  fired after 5 failed probes, "已恢復" ~5 min after recovery (group_interval).
- **Digest** (owner, 2026-10-13: at start + 05:00 / 17:00 UTC+8, was every 12 h from Grafana's start): Alertmanager has no cron,
  so two routes match `digest=all`. Startup route: group_wait 1m, never repeats; the heartbeat carries label `started` = the
  provisioner's start time, because Grafana keeps its notification log in Postgres and would otherwise treat the same alert set
  as already sent after a restart. A Grafana-only restart therefore sends no startup digest; `up` (provisioner + Grafana) does.
  Scheduled route: flushes every minute but only inside the 2-minute active time interval `digest-times` (Asia/Taipei,
  `DIGEST_TIMES` env of the provisioner), repeat 1h. Grafana runs the previously stored alertmanager config for up to ~1 min
  after start, so the first start after a config change can send one message in the old form. Verified 2026-10-13 with
  fake-line: startup digest within 1 min, scheduled one at the window's minute and only once, new `started` after a
  provisioner restart → new startup digest (Grafana must start after the provisioner's first pass: `up`, not `restart`).

## Agent + gateway (mTLS)
Owner's decision (2026-10-13): every machine pushes everything (node_exporter metrics, apps' traces and logs) through one agent;
the observer exposes one entry point and needs no change when a machine is added. OpenTelemetry's agent + gateway pattern.
- **Pure push** (owner, 2026-10-13): the observer never connects to a machine or service. The GCP-era blackbox service
  probes (Service DOWN, Fleet Overview "Services") were removed: they could only reach what the observer reaches. A dead
  machine is detected by silence (Machine DOWN); a service's health is in its traces / logs in OpenObserve.
- **Gateway** (`otel-collector`): OTLP gRPC :4317 only, TLS with `client_ca_file` = a client certificate signed by our CA is
  required. Bound to `${OTEL_GATEWAY_BIND:-127.0.0.1}`; on the observer set `0.0.0.0` (open to any IP). Certificates from
  `pki/gateway/` (SANs = every IP / name agents dial: locally `host.docker.internal`; remote the observer's public IP).
- **Agent** (`agent/`): receives OTLP/HTTP from local apps on 4318, scrapes node_exporter on the machine (label `service_namespace` =
  `SERVICE_NAMESPACE`), detects `host.name` (see Data model) and puts it on everything: traces/logs (replacing the SDK's
  hostname / container id), metrics as `instance` (via service.instance.id) and `host_name`; adds `service.namespace` when
  the app has none; exports OTLP gRPC + client certificate to `GATEWAY_ENDPOINT`. Disk-backed sending queue
  (`file_storage`, volume `agent-queue`), retries up to 1 h. Runs as root in its container (root-owned queue volume, 0600 key).
  Linux: `network_mode: host`; Mac: `docker-compose.mac.yml` (bridge + host.docker.internal, Colima's host network is the VM's).
- **Verified 2026-10-13** with `telemetrygen` against the gateway: no TLS → "server preface: EOF"; TLS without client cert →
  "tls: certificate required"; client cert from another CA → rejected the same way; local-mac cert → accepted. Metrics, traces
  and logs from the Mac arrive via agent → gateway. Gateway stopped 40 s while sclaser-food served 3 requests → all 3 arrived
  after restart with their original timestamps.
- **Revocation**: the collector has no CRL. To cut off one machine: new CA (`rm -r pki; pki.sh init`), new gateway cert, new
  client certs for the machines that stay. Client certs are valid 2 years, the gateway cert 825 days, the CA 10 years.
- **Apps in Docker on a VM** (sclaser-food): the agent (host network) listens on the docker0 address
  (`AGENT_OTLP_BIND=172.17.0.1:4318`); app containers send to `http://host.docker.internal:4318` with
  `extra_hosts: host.docker.internal:host-gateway` (sclaser-food's compose has both). On Linux host-gateway = docker0's IP.
  Verified 2026-10-13 on Docker 29 with host.docker.internal mapped to 172.17.0.1: containers on the default bridge and on a
  compose network both reach it (200). Not testable with host-gateway in Colima: there it resolves to the Mac
  (192.168.5.2), which is why this Mac's agent uses `docker-compose.mac.yml`. Steps: docs/agent-runbook.md step 5.

## Ingest limits (otel-collector/config.yaml)
| limit | value | over the limit |
|---|---|---|
| OTLP request size | 4 MiB (HTTP and gRPC) | request refused (HTTP 400), the app's exporter drops/retries it |
| attributes per span / log / resource / span event | 128 / 128 / 64 / 64 | extra attributes dropped |
| attribute value, log body | 32 KiB (resource: 4 KiB) | truncated |
| series per scrape | 20000 | that scrape fails |
| collector memory | 400 MiB (+100 spike) | new data refused until memory drops |
Verified with oversized payloads (40 KB body, 300 attributes, 5 MB request). OTTL statements are grouped per context on purpose:
a flat list runs in the lowest context it mentions (e.g. per span event) and silently skips spans without events.

## OpenObserve version
Stable releases only (`latest`, v1.0.4 on 2026-10-12). A metadata DB created by a newer version cannot be opened by an older one
("Migration file of version ... is missing"); that is why the stack was reset on 2026-10-12 after a v1.1.0-rc2 detour.
Before any upgrade back up Postgres (`docker exec postgres pg_dump -U observability openobserve > backups/...sql`; `backups/`
is gitignored) — migrations only go forward.

## OpenObserve gotchas (found on v1.0.4; recheck after upgrades)
- Root password must be 8–128 chars with lower, upper, digit and special character, or the container crash-loops.
- The image is distroless (no shell / curl): no compose healthcheck possible; dependents retry instead.
- Dashboard schema v8: layout grid is **192 columns, 17 px rows** (the 48-column examples online are v5). The generator
  writes on a 48-column grid and scales ×4 / ×2.
- Gauge / metric panels with PromQL render an early point of the range, not the newest one ("current value" panels need
  `panel_time_range` 5m). No OpenObserve dashboard uses PromQL now; machine / service panels are in Grafana.
- PromQL: `label_replace`, `group_left`, `unless`, `absent`, `last_over_time` work; **`round(x, 0.1)` does not** (scalar argument).
- **`label_replace` copying a label** (`"$1"` with `"(.*)"`, or `"${1}"`) leaves the new label unset; a fixed value
  (`label_replace(x, "l", "v", "", "")`) works. Copy with `label_join(x, "dst", "", "src")` instead.
- **`or` / `unless` match whole label sets**, not just the `on(...)` labels, once a side went through label_replace /
  label_join: reduce both sides with `max by(<labels>)(...)` first. Until 2026-10-13 this silently made the alert rules
  ignore per-machine thresholds (default always won); fixed in `gen_alerts.py`, verified with own / default / crit cases.
- Dashboard variables of type `query_values` resolve asynchronously; panels show "no data" for a few seconds on first load.
- `PUT /api/{org}/dashboards/{id}` needs `?folder=` and `&hash=` (the current hash from the list call); a dashboard's hash changes on every write.

## Before the first VM deploy (open items, written 2026-10-13)
Plan: observer on the GCP VM (`GCP_aoeskills`), first monitored subjects = this Mac and sclaser-food on it.
Checked against the old stack's AGENTS.md (`~/monitoring` on that VM); decide these before deploying:
- **Resources**: VM has 2 vCPU, 1.9 GB RAM (~770 MB free while the old stack runs), 9.7 GB disk with 2.6 GB free. Local mode
  here uses ~1.1 GB RAM (OpenObserve ~290 MB, Grafana ~340 MB, RustFS ~240 MB, Postgres ~75 MB) and ~3 GB of images. The old
  stack must stop first, prod mode (GCS + Cloud SQL) saves RustFS + Postgres, and OpenObserve's memory caches may need limits.
  `db-init` could reuse the provisioner image (has psql) instead of postgres:17-alpine (415 MB).
- **Port clashes with the old stack** (host network there): Grafana 13000, node-exporter 9100, Postgres 5432.
- **Gateway port 4317 must be open to the Mac** — the old hard rule was "do not change the GCP firewall / open ports"; the
  agent design (owner, 2026-10-13) needs it: the owner opens it (or uses Tailscale, already running on the VM). Gateway cert:
  `scripts/pki.sh gateway <VM IP / tailnet name> ...`; the Mac agent then gets `GATEWAY_ENDPOINT=<that>:4317`.
- **Storage on the VM**: prod mode needs a GCS bucket + HMAC key and a Postgres (Cloud SQL) — not created yet, GCS path never
  tested — or run local mode on the VM as a first test (disk!).
- **Access**: as on the old stack, Caddy on 127.0.0.1:80 by path (`/grafana/`, `/openobserve/`); tunnel
  `ssh -N -L 8080:127.0.0.1:80 GCP_aoeskills`, or Tailscale (`tailscale serve` on the VM forwards tailnet :80 to 127.0.0.1:80).
  Old Caddy also binds 80 there: stop the old stack first.
- **host.name from GCP**: check on the VM that the agent detects the instance name (`aoeskills-first-host`; leave HOST_NAME unset).
- Old stack also scraped the sclaser VM at `34.104.203.72:9100` (node_exporter reachable from outside); when that VM gets an
  agent, close 9100 there.
- Not verified yet: LINE delivery with real credentials (`.env` empty), GCS / Cloud SQL, the agent's node-exporter on a real
  Linux VM, retention actually deleting expired files.

## Web access (Caddy)
`caddy/Caddyfile`: 127.0.0.1:80, `/grafana*` → grafana:3000, `/openobserve*` → openobserve:5080, else 404; paths forwarded
unchanged. Grafana: `GF_SERVER_ROOT_URL=/grafana/` + `GF_SERVER_SERVE_FROM_SUB_PATH`; OpenObserve: `ZO_BASE_URI=/openobserve`,
so **every OpenObserve URL has the prefix**, internal ones too (collector exporters, Grafana datasource, provisioner `O2_URL`).
**No host name is configured** (owner, 2026-10-13: one stack reached over localhost, SSH tunnel and Tailscale): Grafana's root URL
is path-only (verified: login, redirects, `<base href="/grafana/">` work on any host) and the OpenObserve links on Grafana
dashboards are relative: `../openobserve/...` (`o2_links.py`; Grafana rewrites `/openobserve/...` to `/grafana/openobserve/...`). Grafana's share links are therefore relative too. Direct ports 127.0.0.1:13000 / 5080 stay for debugging
(also under the prefixes). Verified 2026-10-13: both UIs render under their paths, collector / Grafana / provisioner work.

## Operating
- Local, everything incl. this Mac's agent: `docker compose -f docker-compose.local.yml up -d` / `down` (volumes kept; `down -v` deletes all data).
  Prod observer: `docker compose -f docker-compose.prod.yml up -d` / `down`. Needs Docker Compose ≥ 2.20 (`include`).
  Why no wrapper script: `include` with `env_file` gives each mode its storage settings, and an including file may extend an
  included service (local adds `depends_on` on its bucket / postgres). Verified 2026-10-13: up, data kept, down removes all.
- Remote machines run only `agent/` (`docker compose up -d` there).
- Change collector config → `docker compose -f docker-compose.local.yml restart otel-collector`. Change an env file → `docker compose -f docker-compose.local.yml up -d` (recreate).
- Query from a shell: PromQL `GET /openobserve/api/default/prometheus/api/v1/query?query=...`; SQL `POST /openobserve/api/default/_search?type=logs|traces|metrics`
  with `{"query":{"sql":..., "start_time":µs, "end_time":µs}}`. Basic auth with the `.env` credentials.
- In this Claude Code setup the `rtk` command rewriter can mangle `curl`/`grep`/`docker logs` output; use `/usr/bin/curl`,
  `/usr/bin/grep`, `rtk proxy docker logs ...` or a small Python script when the output looks truncated.
