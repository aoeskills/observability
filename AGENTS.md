# Observability stack — read this first (written for AI agents)

OpenObserve + Grafana + OpenTelemetry Collector in Docker Compose (this folder), for any organization or project that
wants machine status, service probes, logs and traces with LINE alerts. All state is in an S3-compatible bucket + one
SQL database (Postgres) chosen by env (see "Storage"). The owner writes Chinese (Traditional); answer in the language
they use.

**Docs are generic.** README, `docs/` and this file describe how the system works for any deployment. Facts about one
deployment (host names, IPs, cloud projects, bucket names, current agents, certificate dates, incidents) never go into
them: they belong in `SITE.md` at the repo root (gitignored; read it if it exists before operating a deployment). The
cloud provider, host names and the agent's platform are the deployment's choice; S3 API for the bucket and a SQL
database are requirements; Postgres is the current choice of database; ports in the docs are recommended defaults.

**Terms (README "角色與用詞")**: hosts have two roles. **collector** = the one host running the whole stack
(`docker-compose.prod.yml`: gateway, OpenObserve, Grafana, ...); **agent** = a monitored host running only `agent/`. Data
flows agent → collector only. `otel-collector` / `otel-agent` are the containers; **gateway** = the collector's
`otel-collector` service. One machine can be both (the collector is usually its own agent; local mode runs everything on
one machine). Use these words for roles in code comments and docs, never "observer", and never a specific machine's
name. Owner-facing runbooks: `docs/collector-runbook.md`, `docs/agent-runbook.md`, `docs/decommission-runbook.md`;
`docs/architecture.md` shows only services, connections and protocols (workflows belong in the runbooks).

## Rules
- **Generated content is not in the repo.** Dashboards and alert rules are built at runtime by the `provisioner` service
  (`provisioner/provision.py`) from what exists right now: one OpenObserve "API Service" dashboard per traces stream, Grafana
  links per stream. Change the generators in `provisioner/`, never the UI (UI edits are reverted / cannot be saved), and never
  commit generated JSON. Machines need no generation: Grafana's dashboards and rules find them with PromQL at query time.
- Services listen on **127.0.0.1 only**; the gateway's 4317 is the only port opened to other hosts. `.env` holds the
  credentials (gitignored, chmod 600); never print or commit it.
- Only the gateway (`otel-collector`) holds OpenObserve credentials. Apps send OTLP to their machine's agent (localhost, no
  auth); agents authenticate to the gateway with a client certificate from the collector's own CA (mTLS). See "Agent + gateway".
- **No state in container volumes.** OpenObserve data → S3-API bucket; OpenObserve metadata, Grafana's database and the thresholds
  → one Postgres server (three databases). The only volumes are OpenObserve's WAL, Grafana's plugin cache and the generated
  provisioning files. Start with `docker compose -f docker-compose.local.yml|docker-compose.prod.yml ...`; never put storage
  settings in `.env` (a value there overrides the included `.local.env` / `.prod.env`).
- **Container logs are capped at 50 MB each** (`x-logging` in every compose file: 5 × 10 MB). OpenObserve logs every
  request at INFO (~1.2 GB/day); uncapped it can fill a small disk within days. These logs are only the services' stdout,
  not monitoring data. Keep the cap on every new service.
- Services decide their own request limits; this stack only has to survive whatever they send. The collector
  enforces that (see "Ingest limits"); keep those limits when changing the pipeline.

## Layout
| path | what |
|---|---|
| `docker-compose.yml` | the collector's services; never started alone. Bucket + Postgres settings from env (`O2_S3_*`, `PG_*`, `O2_EXT_AUTH_SALT`), refuses to start if one is missing |
| `docker-compose.local.yml` | local mode, one project: includes `docker-compose.yml` + `docker-compose.local-storage.yml` (env `.local.env`) and the machine's own agent services (`agent/`, env `agent/.env`): one machine as both roles |
| `docker-compose.local-storage.yml` | local only: bucket (RustFS, console 127.0.0.1:9001), bucket-init (creates the bucket), postgres |
| `docker-compose.prod.yml` | prod mode (the collector): includes `docker-compose.yml` with env `.prod.env` (copy `.prod.env.example`, gitignored) |
| `.env` / `.local.env` / `.prod.env` | shared secrets (root, Grafana, LINE) / local storage settings (dev-only, committed) / prod storage settings |
| `SITE.md` | gitignored: this deployment's own facts (see above) |
| `db/init.sql` | run by `db-init` on every start: creates databases `openobserve`, `grafana`, `monitoring` and tables `usage_threshold`, `machine`, `service` if missing |
| `otel-collector/config.yaml` | the gateway: OTLP gRPC + mTLS from agents → OpenObserve stream `ingest` (pipelines move it to one stream per `service.namespace`); scrapes postgres-exporter (thresholds, machine / service inventories) → Prometheus remote write → OpenObserve |
| `retention.json` | days OpenObserve keeps each project's traces / logs; unlisted projects use `O2_RETENTION_DAYS` |
| `agent/` | everything an agent runs (compose + config + `.env.example`; `docker-compose.mac.yml` for macOS): apps' OTLP + local node_exporter + service probes (`blackbox.yml`, targets `probes/*.yml`) → gateway |
| `scripts/pki.sh` | run on the collector: private CA, gateway certificate, `sign` an agent's CSR → `pki/` (gitignored); `client` for local mode |
| `scripts/setup-node-exporter-macos.sh` | node_exporter as a Homebrew service on a macOS agent (flags in `node-exporter/`); Linux agents run node_exporter from `agent/` |
| `provisioner/provision.py` | the runtime loop (every `DASHBOARD_SYNC_INTERVAL` s); stdlib only |
| `provisioner/openobserve_dashboards.py` | OpenObserve "API Service" dashboard for one stream (SQL over traces / logs; the span and field names it expects are in its docstring) |
| `provisioner/grafana/` | Grafana generators (`gen_fleet_overview.py`, `gen_node_details.py` + stock `node-exporter-full-1860.json`, `gen_alerts.py`), `o2_links.py` |
| `grafana/provisioning/` | the static part of Grafana's provisioning (datasources, dashboard provider); the provisioner copies it next to the generated files |
| `postgres-exporter/queries.yaml` | thresholds + machine / service inventories → metrics |
| `docs/collector-runbook.md` | the collector (zh): setup (env + password rules, gateway cert, opening 4317, deploy key), update, restart cost, backup, troubleshooting |
| `docs/agent-runbook.md` | adding an agent (zh): certificate via CSR, `agent/.env`, probes, apps, macOS, checks, troubleshooting, rename / retire |
| `docs/decommission-runbook.md` | removing the stack from a machine (zh): apps, containers, Homebrew node_exporter, volumes incl. anonymous ones, images, gitignored files, test traces, Retire on the collector |
| `docs/architecture.md` | services, connections, protocols and auth only (mermaid + table) |
| `tools/fake_line_server.py` | stand-in for the LINE push API, for alert tests |

## Data model (what queries can rely on)
- **Metrics**: Prometheus names and labels are kept (remote write), so ordinary PromQL works (but see the lowercasing
  gotcha). Every metric name is its own OpenObserve stream, which is why the collector drops metrics nobody uses
  (exporters' `go_*`, postgres-exporter's `pg_*`). Labels: `job`, `instance`, `service_namespace` (the project, see
  "Projects"); service probes (`job="blackbox"`) add `service` and `target` (the probed URL), their `instance` is the
  probing machine. For machines `instance` is the machine name = OpenTelemetry `host.name`, detected by the agent
  (resourcedetection, re-run every minute: `HOST_NAME` in `agent/.env` if set > cloud instance name where a detector
  supports it > OS hostname), also in label `host_name`; traces / logs carry the same `host.name` (OpenObserve
  `service_host_name` / `host_name`). Only the certificate's name stays fixed. Requirement: a machine renamed at the
  provider shows its new name. Fleet Overview shows `instance` as the card name, read from the same series as the
  value: a rename shows with the next sample, the old card is gone within ~5 min. Do not join the card name from a
  separate name metric: then changing `instance` does not change the card.
  macOS has no `node_memory_MemAvailable_bytes`; memory panels use `... or (active + wired + compressed) / total`.
  On macOS `mountpoint="/"` shows the whole APFS container.
- **Traces** (stream per project = `service.namespace`, see "Projects"): `duration` is in **microseconds**. Resource
  attributes are prefixed `service_` (e.g. `service_service_name`, `service_host_name`).
- **Logs** (same project stream): `severity`, `body`, `trace_id`, `span_id`, plus the record's attributes.
  Nested objects are flattened into one column per key; an app should log arbitrary objects (e.g. request bodies) as one
  JSON string, or they keep adding columns (limit 1000 per record).

## Storage
- Requirements: an **S3-API bucket** and a **SQL database**; the stack is configured for Postgres.
- Write path: ingest → WAL on the container's disk (volume `openobserve-wal`) → after `ZO_MAX_FILE_RETENTION_TIME` (600 s) a WAL file
  becomes parquet on the same disk → about 10 min later uploaded to the bucket (`files/<org>/<type>/<stream>/YYYY/MM/DD/HH/`) +
  row in Postgres `file_list`. First data to bucket takes ~20 min.
- Queries merge both: `file_list` → parquet from the bucket, plus the WAL / memory for the newest data. Dashboards are real
  time; a query over the last hour returns the full hour.
- The compactor merges small files and applies retention in the bucket — never add bucket lifecycle rules (`file_list` would point to deleted files).
- Losing the container disk loses only the not-yet-uploaded WAL (≤ ~20 min for a stream that keeps receiving data; a WAL file
  older than 600 s is closed only when the next record for that type arrives, so a sparse stream can stay longer in the WAL.
  Queries are unaffected, they read the WAL). Losing Postgres orphans the bucket (data present, unlisted).
  A fresh container with an empty disk, same bucket + DSN, serves the old logs, metrics and dashboards.
- One Postgres server, three databases (`db/init.sql`): `openobserve` (metadata: schemas, file list, users, dashboards),
  `grafana` (Grafana's own DB: users, alert state), `monitoring` (thresholds, inventories). Separate databases because
  OpenObserve and Grafana each run their own migrations. `db-init` creates them when the user has CREATEDB; otherwise
  create them by hand. `PG_PASSWORD` goes into OpenObserve's DSN URL, so it must be URL-safe (specials only `-._~`).
  `pg_dump` must be at least the server's major version.
- One S3 client for both modes (`ZO_S3_PROVIDER=s3`): RustFS locally, any S3-API endpoint in prod. Provider-specific
  clients (`ZO_S3_PROVIDER=gcs` etc.) ignore the endpoint and cannot be emulated locally, so they are not used; GCS works
  through its S3 XML API with an HMAC key. OpenObserve never creates the bucket (local: `bucket-init`; prod: create it).
- `ZO_EXT_AUTH_SALT` (16–48 bytes) is required on a new install since v1.1.0 (crash-loop with "db init failed" otherwise) and must stay
  the same for the lifetime of the Postgres DB.
- MinIO is not used: its community edition stopped publishing images in 2025-10 and the repo is archived.

## Projects = `service.namespace`
One name end to end: OpenTelemetry's resource attribute **`service.namespace`** is the project. Everything else derives from it:
| where | name |
|---|---|
| app (OTel SDK resource) / agent | `service.namespace`; agent env `SERVICE_NAMESPACE` |
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
  gateway always sets one. A new project therefore needs nothing on the collector: send data, the stream, its dashboard,
  the Grafana links appear; add a line to `retention.json` if it should not keep the default 30 days.
- Retention: per stream `data_retention` (0 = `O2_RETENTION_DAYS`), set by the provisioner from `retention.json`; OpenObserve's
  compactor deletes expired files from the bucket (never bucket lifecycle rules).

## Provisioner (runtime generation)
- Every pass: keep the two routing pipelines as wanted (missing / changed → recreated), set retention from `retention.json`,
  list traces streams in OpenObserve → write Grafana provisioning into volume `grafana-provisioning` (static files from
  `grafana/provisioning/` + Fleet Overview, Node Exporter Details, alert rules) → create / update / delete OpenObserve dashboards
  (marker `[managed: api-service/<stream>]` in the description; dashboards without it are left alone). The Service dropdown
  defaults to the stream's most frequent `service_name` over 7 days. A new stream gets its dashboard and Grafana links
  within one pass; a deleted stream loses both; an API edit of a managed dashboard is reverted on the next pass.
- Grafana re-reads dashboard files every 30 s; datasources and alert rules only on start. Grafana starts after the provisioner's
  first pass (healthcheck on `/out/.ready`, reset on each provisioner start). After changing `gen_alerts.py`:
  `docker compose -f docker-compose.<mode>.yml restart provisioner grafana` (provisioner first; it rewrites the rules file at start).

## Grafana: machines, services, thresholds, alerts
Split: Grafana for machine/service status, thresholds and LINE alerts; OpenObserve for storage and logs/traces. Why not
all in OpenObserve: its HTML panels are sanitized and custom-chart JS is sandboxed (no `fetch`), so no ✎ threshold
editing on cards; enrichment tables are whole-file CSV uploads, alerts cannot read them and gauges cannot draw them; its
alerts stop evaluating while silenced and have no Alertmanager-style routes that re-send the current state, so no
digest. Grafana has **no Prometheus of its own**: its datasource `openobserve` is OpenObserve's Prometheus-compatible API
(`/api/default/prometheus`, basic auth from `.env`).
- Generator specifics forced by OpenObserve: `round(x * 10) / 10` instead of `round(x, 0.1)`, macOS memory branch
  (`or (active + wired + compressed) / total`), datasource uid `openobserve`. Grafana is served at `/grafana` behind Caddy
  (`caddy/Caddyfile`, `GF_SERVER_SERVE_FROM_SUB_PATH`). Machine DOWN and Service DOWN both pend 4m ("every minute, 5 in a row").
- OpenObserve's Prometheus API: `label/<name>/values` needs `match[]` (Grafana sends it when the variable query names a
  metric), no `status/buildinfo` (harmless). Working: label_replace, label_join, group_left, `^ 0`, last_over_time, absent,
  unless (see the label_replace / set-operation quirks under "OpenObserve gotchas").
- A label change on a scrape target leaves the old series "current" for ~5 min (remote write has no staleness markers), so
  joins on `instance` (`group_left(name)`) fail with "duplicate series" for 5 min and the alert rules error (execErrState →
  one alert message without labels). Avoid renaming labels on live targets.
- **Machine inventory** (the usual push-model "loss of signal" design): agents push, so a dead machine is silence, not
  `up = 0`. The provisioner upserts every machine with an `up` sample in the last minute into `monitoring.machine`
  (exported as `machine_registered{instance,service_namespace}`). Same window as the DOWN rule: with a bare `up` (5-min
  lookback) a machine retired within 5 minutes of its last sample would be re-registered.
  Machine DOWN = registered without an `up == 1` sample in the last minute, pending 4m; Fleet Overview shows a red DOWN
  card with a **Retire** button (deletes the machine row and its own thresholds; a machine that sends again is
  re-registered). Both read `machine_registered` over `[1m]` so a retire shows within a minute.
  Usage cards (CPU / Memory / Disk) show a value only while the machine is UP; otherwise they say "No data" (CPU, a
  rate, would otherwise vanish while Memory / Disk keep the last sample for 5 minutes).
  Node Exporter Details' Availability row uses the same DOWN (the stock panels assume the pull model, where a dead
  machine still produces `up = 0`; with push they show UP / 100 % / 0 s while the machine is silent): Status = the
  instant inventory query; availability = successful `up` samples ÷ `machine_registered` samples over the range (both
  scraped every 15 s; a sleeping or stopped machine counts as down); downtime = the difference × 15 s; last seen up from
  `node_time_seconds`.
- **Service inventory**, the same design (services are pushed too): the provisioner upserts every `(instance, service)`
  with a `probe_success` in the last 2 minutes into `monitoring.service` (exported as
  `service_registered{instance,service,service_namespace}`). Service DOWN = registered without a `probe_success == 1` as
  its newest result in the last 2 minutes (`last_over_time(...[2m])`: probes run once a minute, a `[1m]` window would
  sometimes be empty), pending 4m: the probe fails (not HTTP 200) **or** stops arriving. A dead machine therefore fires
  Machine DOWN plus Service DOWN for each of its services. Fleet Overview shows a red DOWN card labelled
  `probe: no data` with a **Retire** button for a registered service without results (a failing service with results
  has no button: it would be re-registered within a minute). Retiring a machine leaves its services (retired
  separately). Identity = machine + `service` label: a changed URL updates the row, a renamed service is a new one. Both
  sides of the `unless` keep exactly `(instance, service, service_namespace)`: OpenObserve ignores `on(...)` there, and a
  `group_left` join for `target` would fail with many-to-many while a probe's URL changes (old and new target both
  show UP for ~30 s).
- Thresholds: database `monitoring`, table `usage_threshold`, edited from the Fleet Overview cards (datasource uid `thresholds-db`);
  postgres-exporter turns it into `usage_threshold_warn/crit{instance,resource}` (collector `honor_labels`) for the alert rules (Grafana cannot use a
  Prometheus-vs-Postgres expression as an alert condition).
- LINE credentials: container env from `.env`, read on **recreate** (`docker compose -f docker-compose.<mode>.yml up -d grafana`), not on restart.
  A new non-empty token replaces the stored one, an empty value keeps the old one.
- Test without LINE: run `fake-line` (see `tools/fake_line_server.py`), then
  `LINE_PUSH_URL=http://fake-line:8080/v2/bot/message/push docker compose -f docker-compose.local.yml up -d --wait provisioner` and
  `docker compose -f docker-compose.local.yml restart grafana`; afterwards the same without `LINE_PUSH_URL`. `ALERTS_MODE=test` instead gives
  1-minute timers and a fake token.
- **Message texts**: alerts state only what the stack can confirm ("超過 5 分鐘沒有回應"), never guessed causes (the
  operator investigates); the thresholds alert is the exception and says what it is (collector-side data). Each rule
  has annotations `summary` (firing text), `resolved_title` + `resolved` (recovery, labels only: Grafana sends the last firing
  annotations) and `digest` (its line in the digest). Usage: only **CRIT** is sent at once; WARN appears only in the digest.
  Resource names / units come from `RESOURCES` in `gen_alerts.py` (one entry per new usage kind). A rule removed from the
  generator goes into `REMOVED_RULES` (`deleteRules`): file provisioning never deletes a rule that left the file.
- **Digest** (at start + `DIGEST_TIMES`, default 05:00 / 17:00 Asia/Taipei): Alertmanager has no cron, so two routes
  match `digest=all`, both flushing every minute but only inside a 2-minute active time interval, repeat 1h:
  `digest-times` (`DIGEST_TIMES` env of the provisioner) and `startup-window` (one dated window 3–5 minutes after
  the provisioner's start). Not group_wait for the startup one: Grafana evaluates a group's rules one after another, and
  Alertmanager flushes a new group at once when an alert older than group_wait arrives (after a restart: everything
  still firing, the previous heartbeat), so the digest would go out before the slower 12-hour rules are in.
  The heartbeat carries label `started` = the provisioner's start time, because Grafana keeps its notification log in
  Postgres and would otherwise treat the same alert set as already sent after a restart. A Grafana-only restart therefore
  sends no startup digest; `up` (provisioner + Grafana) does. Grafana runs the previously stored alertmanager config for up to
  ~1 min after start, so the first start after a config change can send one message in the old form.
  Content: "目前有問題" (machine → service → usage CRIT → thresholds), then "📋 過去 12 小時彙整" with
  sections 【機器】【服務】【用量超標】 (empty ones left out; all empty → one line "沒有停機，用量也沒有超標"), at most 20 lines
  per section (LINE: 5000 characters per message, a longer one is refused as a whole).
- **12-hour report rules** (`kind=report`, so only the digest routes take them): `machine-downtime` / `service-downtime`
  (minutes registered − minutes up, only machines / services still registered; availability = 100 − minutes / 7.2) and
  `usage-12h` (minutes ≥ warn, of them ≥ crit, highest value, latest thresholds). Range queries over 12 h at 1-minute steps,
  reduced by Grafana (count / sum / max / last): OpenObserve's subqueries and range-mode set operators do not work (see
  gotchas). Every query of a rule must return series: Grafana marks the whole rule NoData when any one is empty (an empty
  "minutes over crit" would hide the usage report), hence `>= bool` and `up or registered * 0`.
  To test with past data, back-fill through `/api/default/ingest/metrics/_json` (≤ 5 h old, see gotchas).

## Agent + gateway (mTLS)
Every machine pushes everything (node_exporter metrics, apps' traces and logs) through one agent; the collector exposes one
entry point and needs no change when an agent is added. OpenTelemetry's agent + gateway pattern.
- **Pure push**: the collector never connects to an agent or service. Service probes run on the service's own machine:
  the agent's blackbox-exporter (`agent/blackbox.yml`, only HTTP 200 = up) probes the targets in `agent/probes/*.yml`
  (file_sd, per machine, gitignored; `example.yml.sample`) once a minute and pushes `probe_success` (kept:
  `probe_success|probe_http_status_code|probe_duration_seconds`). Service DOWN: see "Service inventory".
- **Gateway** (`otel-collector`): OTLP gRPC :4317 only, TLS with `client_ca_file` = a client certificate signed by the CA is
  required. Bound to `${OTEL_GATEWAY_BIND:-127.0.0.1}`; on a collector with remote agents set `0.0.0.0`. Certificates from
  `pki/gateway/` (SANs = every IP / name agents dial: locally `host.docker.internal`; remote the collector's address + `127.0.0.1` for its own agent).
  On a Linux collector the `otel-collector` container's user (uid 10001) must own the key: `sudo chown 10001:10001 pki/gateway/gateway.key` (keep 0600),
  else "open /certs/gateway.key: permission denied" and a restart loop (Docker Desktop / Colima on macOS ignore file owners).
  **Signing is the collector's job**: the CA lives in the collector's `pki/` (`scripts/pki.sh init`),
  the gateway's key + cert are made there (`pki.sh gateway <SANs>`), and an agent's key + CSR are made on the agent and
  signed by the collector (`ssh <agent> cat csr | ssh <collector> scripts/pki.sh sign | ssh <agent> 'cat > client.crt'`,
  agent runbook step 1); keys never travel. `pki.sh client` (key + cert in one place) is for local mode only, where one
  machine is the collector with its own CA. Back up `pki/ca.key` off the collector: losing it means re-issuing every certificate.
- **Agents get no clone of this repo**: only the files agent runbook step 2 copies, into `~/agent`, driven over ssh from
  the operator's clone; the collector's own agent too (`~/agent`, not the repo's `agent/`). Changes go operator's clone →
  push → `git pull` on the collector (read-only deploy key).
- **Agent** (`agent/`, `otel-agent`): receives OTLP/HTTP from local apps on 4318, scrapes node_exporter on the machine (label `service_namespace` =
  `SERVICE_NAMESPACE`), detects `host.name` (see Data model) and puts it on everything: traces/logs (replacing the SDK's
  hostname / container id), metrics as `instance` (via service.instance.id) and `host_name`; adds `service.namespace` when
  the app has none; exports OTLP gRPC + client certificate to `GATEWAY_ENDPOINT`. Disk-backed sending queue
  (`file_storage`, volume `agent-queue`), retries up to 1 h, so a gateway outage loses nothing shorter than that (data
  arrives later with original timestamps). Runs as root in its container (root-owned queue volume, 0600 key).
  Linux: `network_mode: host`; macOS: `docker-compose.mac.yml` (bridge + host.docker.internal: Docker's host network on
  macOS is the VM's) and node_exporter as a Homebrew service.
- mTLS behaviour: no TLS → "server preface: EOF"; TLS without client cert → "tls: certificate required"; client cert from
  another CA → rejected the same way.
- **Revocation**: the collector has no CRL. To cut off one machine: on the collector a new CA (move `pki/` away, `pki.sh init`),
  new gateway cert, new client certs for the machines that stay. Client certs are valid 2 years, the gateway cert 825 days, the CA 10 years.
- **Apps in Docker on a Linux agent**: the agent (host network) listens on the docker0 address
  (`AGENT_OTLP_BIND=172.17.0.1:4318`); app containers send to `http://host.docker.internal:4318` with
  `extra_hosts: host.docker.internal:host-gateway`. On Linux host-gateway = docker0's IP; containers on the default bridge
  and on compose networks both reach it. Not on macOS: there host-gateway resolves to the Mac itself, hence
  `docker-compose.mac.yml`. Steps: docs/agent-runbook.md step 5.

## Ingest limits (otel-collector/config.yaml)
| limit | value | over the limit |
|---|---|---|
| OTLP request size | 4 MiB (HTTP and gRPC) | request refused (HTTP 400), the app's exporter drops/retries it |
| attributes per span / log / resource / span event | 128 / 128 / 64 / 64 | extra attributes dropped |
| attribute value, log body | 32 KiB (resource: 4 KiB) | truncated |
| series per scrape | 20000 | that scrape fails |
| collector memory | 400 MiB (+100 spike) | new data refused until memory drops |
OTTL statements are grouped per context on purpose: a flat list runs in the lowest context it mentions (e.g. per span
event) and silently skips spans without events.

## OpenObserve version
Stable releases only (`latest`). A metadata DB created by a newer version cannot be opened by an older one
("Migration file of version ... is missing"), so never try a release candidate on a DB you keep. Before any upgrade
back up Postgres (collector runbook step 8; `backups/` is gitignored) — migrations only go forward.

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
  label_join: reduce both sides with `max by(<labels>)(...)` first. Otherwise per-machine thresholds are silently
  ignored (the default always wins); `gen_alerts.py` does this.
- **`rate()` over a partly filled window** (an agent that just started or stopped) behaves as if divided by the whole
  window: `1 - avg(rate(idle[5m]))` shows 80-90 % CPU for ~5 min after every agent start / stop (e.g. a laptop
  sleeping and waking) and fires Usage over CRIT. CPU is therefore `1 - sum(rate(idle)) / sum(rate(all modes))` over the
  same samples, where the error cancels (Fleet Overview + alerts). The stock Node Exporter Details dashboard still has
  the old form (graph edges only, no alerts).
- **`or` / `unless` in range queries pick whole series, not timestamps**: `up or registered * 0` with equal labels returns
  only the `up` points (no 0 where `up` is missing), `registered unless up` returns nothing once `up` has any point in
  the range. Instant queries (Fleet Overview, alert rules) are correct. Graphs merge per timestamp in Grafana instead
  (Node Exporter Details "Up / Down over time": outer join on Time + reduce row max).
- **Subqueries are broken**: `x[2h:1m]` on a selector → HTTP 400; on an expression x is evaluated once
  (`count_over_time(m[1h:1m])` = 1, not 60), and it fails with "Unsupported subquery" when x contains rate().
  `timestamp(x)` returns the evaluation time, not the sample's. Use `*_over_time` on plain selectors (sample counts), a
  metric whose value is a time (`node_time_seconds`), or a range query + Grafana reduce with `>= bool`.
- **Range results are cached** (on disk, survives a restart): data back-filled into an already queried time range stays
  invisible to the same query string (`[1m]` vs `[60s]` gave 6 vs 241 points). Real data arrives in order; for tests with
  back-filled data use equivalent but different durations.
- **Metric names are lowercased**, by JSON metrics ingest (`/api/{org}/ingest/metrics/_json`) and by remote write:
  `node_memory_MemAvailable_bytes` / `MemTotal_bytes` exist only as `node_memory_memavailable_bytes` /
  `memtotal_bytes`, and PromQL with the mixed-case name finds nothing. Generators therefore write lowercase names
  (`gen_node_details.py` lowercases every `node_*` name of the stock dashboard). JSON ingest also refuses data older than
  `ZO_INGEST_ALLOWED_UPTO` (default 5 h).
- Dashboard variables of type `query_values` resolve asynchronously; panels show "no data" for a few seconds on first load.
- `PUT /api/{org}/dashboards/{id}` needs `?folder=` and `&hash=` (the current hash from the list call); a dashboard's hash changes on every write.

## Web access (Caddy)
`caddy/Caddyfile`: 127.0.0.1:80, `/grafana*` → grafana:3000, `/openobserve*` → openobserve:5080, else 404; paths forwarded
unchanged. Grafana: `GF_SERVER_ROOT_URL=/grafana/` + `GF_SERVER_SERVE_FROM_SUB_PATH`; OpenObserve: `ZO_BASE_URI=/openobserve`,
so **every OpenObserve URL has the prefix**, internal ones too (collector exporters, Grafana datasource, provisioner `O2_URL`).
**No host name is configured**: the stack must work behind any entry (localhost, SSH tunnel, VPN, a reverse proxy).
Grafana's root URL is path-only (login, redirects, `<base href="/grafana/">` work on any host) and the OpenObserve links
on Grafana dashboards are relative: `../openobserve/...` (`o2_links.py`; Grafana rewrites `/openobserve/...` to
`/grafana/openobserve/...`). Grafana's share links are therefore relative too. Direct ports 127.0.0.1:13000 (Grafana) /
5080 (OpenObserve) exist for debugging (also under the prefixes); user docs mention only the paths.

## Operating
- Local, everything incl. the machine's own agent: `docker compose -f docker-compose.local.yml up -d` / `down` (volumes kept; `down -v` deletes all data).
  Prod collector: `docker compose -f docker-compose.prod.yml up -d` / `down`. Needs Docker Compose ≥ 2.20 (`include`).
  Why no wrapper script: `include` with `env_file` gives each mode its storage settings, and an including file may extend an
  included service (local adds `depends_on` on its bucket / postgres).
- Agents run only `~/agent` (`docker compose up -d` there; macOS adds `-f docker-compose.mac.yml`).
- Change collector config → `docker compose -f docker-compose.<mode>.yml restart otel-collector`. Change an env file → `docker compose -f docker-compose.<mode>.yml up -d` (recreate).
- Query from a shell: PromQL `GET /openobserve/api/default/prometheus/api/v1/query?query=...`; SQL `POST /openobserve/api/default/_search?type=logs|traces|metrics`
  with `{"query":{"sql":..., "start_time":µs, "end_time":µs}}`. Basic auth with the `.env` credentials.
- LINE: a 401 in Grafana's log ("Notify for alerts failed ... 401") = invalid channel token; check a token without
  sending: `GET https://api.line.me/v2/bot/info` with `Authorization: Bearer <token>` (200 = valid). Test a contact point
  on Grafana 13: `POST /grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/<base64url(name)>/test`
  with `{"integration": <the receiver's stored integration>, "alert": {...}}` (the old `/api/alertmanager/.../receivers/test` → 410).
