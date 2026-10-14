# Observability stack — read this first (written for AI agents)

OpenObserve + Grafana + OpenTelemetry Collector in Docker Compose (this folder), node_exporter as a Homebrew service on the Mac.
Deployed since 2026-10-13: the collector runs in prod mode on the GCP VM (see "Deployment"); all state is in a bucket + one
Postgres server chosen by env (see "Storage"). Agents (since 2026-10-15): the collector VM itself (`aoeskills-first-host`,
project `observability`) and the sclaser VM (`instance-20260804-060650`, sclaser-food in Docker). This Mac's agent
(`local-mac`) is off: its certificate died with the CA change of 2026-10-15.
The old Prometheus + Grafana stack that ran on that VM (`~/monitoring`) was removed the same day; its configs are archived on
the Mac in `backups/gcp-monitoring-configs-20261013.tar.gz`; its alert / threshold / Fleet Overview design lives on in
`provisioner/grafana/`. The owner writes Chinese (Traditional); answer in the language they use.

**Terms (owner, 2026-10-13; README "角色與用詞")**: hosts have two roles. **collector** = the one host running the whole stack
(`docker-compose.prod.yml`: gateway, OpenObserve, Grafana, ...); **agent** = a monitored host running only `agent/`. Data flows
agent → collector only. `otel-collector` / `otel-agent` are the containers; **gateway** = the collector's `otel-collector`
service. One machine can be both (local mode). Use these words for roles in code comments and docs, never "observer", and
"VM" / "Mac" only for a specific machine. Owner-facing runbooks: `docs/collector-runbook.md`, `docs/agent-runbook.md`, `docs/decommission-runbook.md`;
`docs/architecture.md` shows only services, connections and protocols (workflows belong in the runbooks).

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
- **Container logs are capped at 50 MB each** (`x-logging` in every compose file: 5 × 10 MB). OpenObserve logs every
  request at INFO (~1.2 GB/day); uncapped it took 1.6 GB of the VM's 9.7 GB disk in 33 h (found 2026-10-15). These logs
  are only the services' stdout, not monitoring data. Keep the cap on every new service.
- Services decide their own request limits; this stack only has to survive whatever they send. The collector
  enforces that (see "Ingest limits"); keep those limits when changing the pipeline.

## Layout
| path | what |
|---|---|
| `docker-compose.yml` | the collector's services; never started alone. Bucket + Postgres settings from env (`O2_S3_*`, `PG_*`, `O2_EXT_AUTH_SALT`), refuses to start if one is missing |
| `docker-compose.local.yml` | local mode, one project: includes `docker-compose.yml` + `docker-compose.local-storage.yml` (env `.local.env`) and this Mac's agent services (`agent/`, env `agent/.env`): one machine as both roles |
| `docker-compose.local-storage.yml` | local only: bucket (RustFS, console 127.0.0.1:9001), bucket-init (creates the bucket), postgres |
| `docker-compose.prod.yml` | prod mode (the collector): includes `docker-compose.yml` with env `.prod.env` (copy `.prod.env.example`, gitignored) |
| `.env` / `.local.env` / `.prod.env` | shared secrets (root, Grafana, LINE) / local storage settings (dev-only, committed) / prod storage settings |
| `db/init.sql` | run by `db-init` on every start: creates databases `openobserve`, `grafana`, `monitoring` and tables `usage_threshold`, `machine`, `service` if missing |
| `otel-collector/config.yaml` | the gateway: OTLP gRPC + mTLS from agents → OpenObserve stream `ingest` (pipelines move it to one stream per `service.namespace`); scrapes postgres-exporter (thresholds, machine / service inventories) → Prometheus remote write → OpenObserve |
| `retention.json` | days OpenObserve keeps each project's traces / logs (`sclaser_food`: 90); unlisted projects use `O2_RETENTION_DAYS` |
| `agent/` | everything an agent runs (compose + config + `.env.example`; `docker-compose.mac.yml` for this Mac): apps' OTLP + local node_exporter + service probes (`blackbox.yml`, targets `probes/*.yml`) → gateway |
| `scripts/pki.sh` | run on the collector: private CA, gateway certificate, `sign` an agent's CSR → `pki/` (gitignored); `client` for local mode |
| `provisioner/provision.py` | the runtime loop (every `DASHBOARD_SYNC_INTERVAL` s); stdlib only |
| `provisioner/openobserve_dashboards.py` | OpenObserve "API Service" dashboard for one stream (SQL over traces / logs) |
| `provisioner/grafana/` | Grafana generators ported from the GCP stack (`gen_fleet_overview.py`, `gen_node_details.py` + stock `node-exporter-full-1860.json`, `gen_alerts.py`), `o2_links.py` |
| `grafana/provisioning/` | the static part of Grafana's provisioning (datasources, dashboard provider); the provisioner copies it next to the generated files |
| `postgres-exporter/queries.yaml` | thresholds + machine / service inventories → metrics |
| `node-exporter/` | flags for this Mac's brew service (`scripts/setup-node-exporter-macos.sh`); Linux agents run node_exporter from `agent/` |
| `docs/collector-runbook.md` | the collector (zh): setup (env + password rules, gateway cert via CSR, firewall, deploy key), update, restart cost, backup, troubleshooting |
| `docs/agent-runbook.md` | adding an agent (zh): certificate via CSR, `agent/.env`, probes, apps, checks, troubleshooting, rename / retire |
| `docs/decommission-runbook.md` | removing the stack from a machine (zh): apps, containers, Homebrew node_exporter, volumes incl. anonymous ones, images, gitignored files, temp / test traces, Retire on the collector |
| `docs/architecture.md` | services, connections, protocols and auth only (mermaid + table) |
| `tools/fake_line_server.py` | stand-in for the LINE push API, for alert tests |

## Data model (what queries can rely on)
- **Metrics**: Prometheus names and labels are kept (remote write), so PromQL from the old stack works. Every metric name is
  its own OpenObserve stream, which is why the collector drops metrics nobody uses (exporters' `go_*`, postgres-exporter's
  `pg_*`). Labels: `job`, `instance`, `service_namespace` (the project, see "Projects"); service probes (`job="blackbox"`) add
  `service` and `target` (the probed URL), their `instance` is the probing machine. For machines `instance` is the
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
- Losing the container disk loses only the not-yet-uploaded WAL (≤ ~20 min for a stream that keeps receiving data; a WAL file
  older than 600 s is closed only when the next record for that type arrives — observed 2026-10-13: a sparse logs stream's file
  opened 08:49 was closed 09:06 by the next log, uploaded 09:17. Queries are unaffected, they read the WAL). Losing Postgres orphans the bucket (data present, unlisted).
  Verified 2026-10-12: a fresh container with an empty disk, same bucket + DSN, served the old logs, metrics and dashboards.
- One Postgres server, three databases (`db/init.sql`): `openobserve` (metadata: schemas, file list, users, dashboards),
  `grafana` (Grafana's own DB: users, alert state), `monitoring` (thresholds). Separate databases because OpenObserve and
  Grafana each run their own migrations. `db-init` creates them when the user has CREATEDB; otherwise create them by hand.
  `PG_PASSWORD` goes into OpenObserve's DSN URL, so it must be URL-safe.
- One S3 client for both modes (`ZO_S3_PROVIDER=s3`): RustFS locally, GCS through its S3 XML API with an HMAC key in prod
  (`ZO_S3_PROVIDER=gcs` would switch to the native GCS client, which ignores the endpoint, so it cannot be emulated locally).
  Verified on GCS 2026-10-13 (prod, bucket `aoeskills_bucket-1`, asia-northeast1): metrics, traces and logs uploaded as
  parquet under `files/default/<type>/<stream>/YYYY/MM/DD/HH/`. OpenObserve never creates the bucket (local: `bucket-init`; prod: create it in GCP).
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
  gateway always sets one. A new project therefore needs nothing on the collector: send data, the stream, its dashboard,
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
Split decided by the owner (2026-10-12): Grafana for machine/service status, thresholds and LINE alerts (the GCP design, ported);
OpenObserve for storage and logs/traces. Why not all in OpenObserve (tried 2026-10-12): its HTML panels are sanitized and
custom-chart JS is sandboxed (no `fetch`), so no ✎ threshold editing on cards; enrichment tables are whole-file CSV uploads,
alerts cannot read them and gauges cannot draw them; its alerts stop evaluating while silenced and have no Alertmanager-style
routes that re-send the current state, so no digest. Grafana has **no Prometheus of its own**: its datasource `openobserve` is
OpenObserve's Prometheus-compatible API (`/api/default/prometheus`, basic auth from `.env`).
- Generators in `provisioner/grafana/` are the GCP ones with these changes: `round(x, 0.1)` → `round(x * 10) / 10` (OpenObserve
  rejects the 2-arg form), macOS memory branch (`or (active + wired + compressed) / total`), Grafana served at `/grafana` behind Caddy as on GCP
  (`caddy/Caddyfile`, `GF_SERVER_SERVE_FROM_SUB_PATH`), datasource uid `openobserve`, machine-down pending 4m like service-down (owner: both "every minute, 5 in a row").
- OpenObserve's Prometheus API: `label/<name>/values` needs `match[]` (Grafana sends it when the variable query names a
  metric), no `status/buildinfo` (harmless). Tested: label_replace, label_join, group_left, `^ 0`, last_over_time, absent, unless
  (see the label_replace / set-operation quirks under "OpenObserve gotchas").
- A label change on a scrape target leaves the old series "current" for ~5 min (remote write has no staleness markers), so
  joins on `instance` (`group_left(name)`) fail with "duplicate series" for 5 min and the alert rules error (execErrState →
  one alert message without labels). Avoid renaming labels on live targets.
- **Machine inventory** (owner's decision 2026-10-13, the usual push-model "loss of signal" design): agents push, so a
  dead machine is silence, not `up = 0`. The provisioner upserts every machine with an `up` sample in the last minute into
  `monitoring.machine` (exported as `machine_registered{instance,service_namespace}`). Same window as the DOWN rule: with
  a bare `up` (5-min lookback) a machine retired within 5 minutes of its last sample was re-registered (fixed 2026-10-13).
  Machine DOWN = registered without an `up == 1` sample in the last minute, pending 4m; Fleet Overview shows a red DOWN
  card with a **Retire** button (deletes the machine row and its own thresholds; a machine that sends again is
  re-registered). Both read `machine_registered` over `[1m]` so a retire shows within a minute. Verified 2026-10-13: agent
  stopped → DOWN card in 1 min, alert firing after ~6 min; Retire via the button's SQL → card + alert gone in 1 min; agent
  started → registered again, UP.
  Usage cards (CPU / Memory / Disk) show a value only while the machine is UP; otherwise they say "No data" (owner,
  2026-10-14). Before, CPU (a rate: needs two samples in its 5 min) vanished after an agent start / stop while Memory /
  Disk kept the last sample for 5 minutes. Checked 2026-10-14 against the VM's data at test instants (query results only).
  Node Exporter Details' Availability row uses the same DOWN (it was written for the pull model, where a dead machine
  still produced `up = 0`; with push it showed UP / 100 % / 0 s while the machine was silent): Status now = the instant
  inventory query; availability = successful `up` samples ÷ `machine_registered` samples over the range (both scraped
  every 15 s; a sleeping or stopped machine counts as down); downtime = the difference × 15 s; last seen up from
  `node_time_seconds`. Checked 2026-10-14 on the VM's data (DOWN now: 0 / 50.8 % / 1770 s over 1 h, old panels UP /
  100 % / 0 s); the timeline's transformations rendered in a scratch Grafana 13.2.3 with the same series.
- **Service inventory**, the same design (owner, 2026-10-13: services are pushed too, so they get the same treatment): the
  provisioner upserts every `(instance, service)` with a `probe_success` in the last 2 minutes into `monitoring.service`
  (exported as `service_registered{instance,service,service_namespace}`). Service DOWN = registered without a
  `probe_success == 1` as its newest result in the last 2 minutes (`last_over_time(...[2m])`: probes run once a minute, a
  `[1m]` window would sometimes be empty), pending 4m: the probe fails (not HTTP 200) **or** stops arriving. A dead
  machine therefore fires Machine DOWN plus Service DOWN for each of its services (owner's choice). Fleet Overview shows a
  red DOWN card labelled `probe: no data` with a **Retire** button for a registered service without results (a failing
  service with results has no button: it would be re-registered within a minute). Retiring a machine leaves its services
  (owner: retired separately). Identity = machine + `service` label: a changed URL updates the row, a renamed service is a
  new one. Both sides of the `unless` keep exactly `(instance, service, service_namespace)`: OpenObserve ignores `on(...)`
  there (rechecked 2026-10-13), and a `group_left` join for `target` would fail with many-to-many while a probe's URL
  changes. Verified 2026-10-13 in local mode (`ALERTS_MODE=test` + fake-line): failing probe → registered, card 0, alert
  firing; probe file removed → card `probe: no data`, alert stays firing (no "recovered" in between); Retire via the
  button's SQL within 5 min of the last result → not re-registered, card gone, alert recovered; agent stopped → Machine
  DOWN + Service DOWN firing, agent started → both recovered; URL changed → no DOWN (old and new target both shown UP for
  ~30 s). The cards' HTML / Retire button not yet looked at in a browser.
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
- **Message texts** (owner, 2026-10-14): alerts state only what the stack can confirm ("超過 5 分鐘沒有回應"), never guessed
  causes (the owner investigates); the thresholds alert is the exception and says what it is (collector-side data). Each rule
  has annotations `summary` (firing text), `resolved_title` + `resolved` (recovery, labels only: Grafana sends the last firing
  annotations) and `digest` (its line in the digest). Usage: only **CRIT** is sent at once; WARN appears only in the digest.
  Resource names / units come from `RESOURCES` in `gen_alerts.py` (one entry per new usage kind). A rule removed from the
  generator goes into `REMOVED_RULES` (`deleteRules`): file provisioning never deletes a rule that left the file (the old
  `usage-warn` stayed in Grafana until then).
- **Digest** (owner, 2026-10-13: at start + 05:00 / 17:00 UTC+8, was every 12 h from Grafana's start): Alertmanager has no cron,
  so two routes match `digest=all`, both flushing every minute but only inside a 2-minute active time interval, repeat 1h:
  `digest-times` (Asia/Taipei, `DIGEST_TIMES` env of the provisioner) and `startup-window` (one dated window 3–5 minutes after
  the provisioner's start). Not group_wait for the startup one (until 2026-10-14): Grafana evaluates a group's rules one
  after another, and Alertmanager flushes a new group at once when an alert older than group_wait arrives (after a restart:
  everything still firing, the previous heartbeat), so the startup digest went out before the slower 12-hour rules were in.
  The heartbeat carries label `started` = the provisioner's start time, because Grafana keeps its notification log in
  Postgres and would otherwise treat the same alert set as already sent after a restart. A Grafana-only restart therefore
  sends no startup digest; `up` (provisioner + Grafana) does. Grafana runs the previously stored alertmanager config for up to
  ~1 min after start, so the first start after a config change can send one message in the old form.
  Content (owner, 2026-10-14): "目前有問題" (machine → service → usage CRIT → thresholds), then "📋 過去 12 小時彙整" with
  sections 【機器】【服務】【用量超標】 (empty ones left out; all empty → one line "沒有停機，用量也沒有超標"), at most 20 lines
  per section (LINE: 5000 characters per message, a longer one is refused as a whole).
- **12-hour report rules** (`kind=report`, so only the digest routes take them): `machine-downtime` / `service-downtime`
  (minutes registered − minutes up, only machines / services still registered; availability = 100 − minutes / 7.2) and
  `usage-12h` (minutes ≥ warn, of them ≥ crit, highest value, latest thresholds). Range queries over 12 h at 1-minute steps,
  reduced by Grafana (count / sum / max / last): OpenObserve's subqueries and range-mode set operators do not work (see
  gotchas). Every query of a rule must return series: Grafana marks the whole rule NoData when any one is empty (an empty
  "minutes over crit" hid the usage report), hence `>= bool` and `up or registered * 0`.
  Verified 2026-10-14 in local mode with fake data back-filled through `/api/default/ingest/metrics/_json` (4 h; scenarios:
  30-min gap + 10 min `up=0` → 39 min, retired machine left out, failing / silent probes → 18 and 10 min, memory over an own
  warn of 70 for 36 min of which 6 over crit 85, peak 90; CRIT immediate text with its threshold), all texts read back from
  rule annotations; startup digest with all three sections sent once inside its window; Machine DOWN immediate text with fake-line.

## Agent + gateway (mTLS)
Owner's decision (2026-10-13): every machine pushes everything (node_exporter metrics, apps' traces and logs) through one agent;
the collector exposes one entry point and needs no change when an agent is added. OpenTelemetry's agent + gateway pattern.
- **Pure push** (owner, 2026-10-13): the collector never connects to an agent or service. Service probes run on the
  service's own machine: the agent's blackbox-exporter (`agent/blackbox.yml`, only HTTP 200 = up) probes the targets in
  `agent/probes/*.yml` (file_sd, per machine, gitignored; `example.yml.sample`) once a minute and pushes `probe_success`
  (kept: `probe_success|probe_http_status_code|probe_duration_seconds`). Service DOWN: see "Service inventory" (failing or
  silent for 5 minutes; a dead machine fires it for each of its services). The gateway's own blackbox job was removed:
  it could only reach what the collector reaches (the Mac's app only while the Mac was the collector too).
- **Gateway** (`otel-collector`): OTLP gRPC :4317 only, TLS with `client_ca_file` = a client certificate signed by our CA is
  required. Bound to `${OTEL_GATEWAY_BIND:-127.0.0.1}`; on the collector set `0.0.0.0` (open to any IP). Certificates from
  `pki/gateway/` (SANs = every IP / name agents dial: locally `host.docker.internal`; remote the collector's public IP + `127.0.0.1` for its own agent).
  On a Linux collector the `otel-collector` container's user (uid 10001) must own the key: `sudo chown 10001:10001 pki/gateway/gateway.key` (keep 0600),
  else "open /certs/gateway.key: permission denied" and a restart loop (seen 2026-10-13; Colima on the Mac ignores file owners).
  **Signing is the collector's job** (owner, 2026-10-15): the CA lives in the collector's `pki/` (`scripts/pki.sh init`),
  the gateway's key + cert are made there (`pki.sh gateway <SANs>`), and an agent's key + CSR are made on the agent and
  signed by the collector (`ssh <agent> cat csr | ssh <collector> scripts/pki.sh sign | ssh <agent> 'cat > client.crt'`,
  agent runbook step 1); keys never travel. `pki.sh client` (key + cert in one place) is for local mode only, where the
  Mac is the collector with its own CA.
- **Agent** (`agent/`, `otel-agent`): receives OTLP/HTTP from local apps on 4318, scrapes node_exporter on the machine (label `service_namespace` =
  `SERVICE_NAMESPACE`), detects `host.name` (see Data model) and puts it on everything: traces/logs (replacing the SDK's
  hostname / container id), metrics as `instance` (via service.instance.id) and `host_name`; adds `service.namespace` when
  the app has none; exports OTLP gRPC + client certificate to `GATEWAY_ENDPOINT`. Disk-backed sending queue
  (`file_storage`, volume `agent-queue`), retries up to 1 h. Runs as root in its container (root-owned queue volume, 0600 key).
  Linux: `network_mode: host`; Mac: `docker-compose.mac.yml` (bridge + host.docker.internal, Colima's host network is the VM's).
- **Verified 2026-10-13** with `telemetrygen` against the gateway: no TLS → "server preface: EOF"; TLS without client cert →
  "tls: certificate required"; client cert from another CA → rejected the same way; local-mac cert → accepted. Metrics, traces
  and logs from the Mac arrive via agent → gateway. Gateway stopped 40 s while sclaser-food served 3 requests → all 3 arrived
  after restart with their original timestamps.
- **Revocation**: the collector has no CRL. To cut off one machine: on the collector a new CA (move `pki/` away, `pki.sh init`),
  new gateway cert, new client certs for the machines that stay. Client certs are valid 2 years, the gateway cert 825 days, the CA 10 years.
- **Apps in Docker on a Linux agent** (sclaser-food): the agent (host network) listens on the docker0 address
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
- **`rate()` over a partly filled window** (an agent that just started or stopped) behaves as if divided by the whole
  window: `1 - avg(rate(idle[5m]))` showed 80-90 % CPU for ~5 min after every agent start / stop and fired Usage over CRIT
  (seen 2026-10-13 while the Mac slept and woke). CPU is therefore `1 - sum(rate(idle)) / sum(rate(all modes))` over the
  same samples, where the error cancels (Fleet Overview + alerts; verified on the same data: 83 → 31 % at a start, 91 →
  17 % at a stop, equal with a full window). The stock Node Exporter Details dashboard still has the old form (graph
  edges only, no alerts).
- **`or` / `unless` in range queries pick whole series, not timestamps**: `up or registered * 0` with equal labels returns
  only the `up` points (no 0 where `up` is missing), `registered unless up` returns nothing once `up` has any point in
  the range. Instant queries (Fleet Overview, alert rules) are correct. Graphs merge per timestamp in Grafana instead
  (Node Exporter Details "Up / Down over time": outer join on Time + reduce row max). Found 2026-10-14.
- **No subqueries**: `x[2h:1m]` on a selector → HTTP 400; on an expression only one point is evaluated (the old
  "Last seen up" panel always showed 0). `timestamp(x)` returns the evaluation time, not the sample's. Use
  `*_over_time` on plain selectors (sample counts) or a metric whose value is a time (`node_time_seconds`).
- **Subqueries are broken**: `x[12h:1m]` evaluates x once (`count_over_time(m[1h:1m])` = 1, not 60), and fails with
  "Unsupported subquery" when x contains rate(). In **range queries** `unless` / `or` act on whole series, not per step
  (`registered unless up` returned nothing while 8 of 112 minutes had no up). Instead: range query + Grafana reduce, `>= bool`.
- **Range results are cached** (on disk, survives a restart): data back-filled into an already queried time range stays
  invisible to the same query string (`[1m]` vs `[60s]` gave 6 vs 241 points). Real data arrives in order; for tests with
  back-filled data use equivalent but different durations.
- JSON metrics ingest (`/api/{org}/ingest/metrics/_json`) lowercases metric names (`node_memory_MemTotal_bytes` →
  `node_memory_memtotal_bytes`), and older than `ZO_INGEST_ALLOWED_UPTO` (default 5 h) is refused. **Remote write lowercases them
  too** (found 2026-10-15 with the first Linux agents): `node_memory_MemAvailable_bytes` / `MemTotal_bytes` exist only as
  `node_memory_memavailable_bytes` / `memtotal_bytes`, and PromQL with the mixed-case name finds nothing, so the Linux
  memory branch of Fleet Overview / alerts / Node Exporter Details is empty (open: generators to use the lowercase names).
- Dashboard variables of type `query_values` resolve asynchronously; panels show "no data" for a few seconds on first load.
- `PUT /api/{org}/dashboards/{id}` needs `?folder=` and `&hash=` (the current hash from the list call); a dashboard's hash changes on every write.

## Deployment (GCP VM, since 2026-10-13)
- **Collector**: `ssh GCP_aoeskills` = `aoeskills-first-host` (project `united-rope-297316`, zone asia-northeast1-b, external IP
  `34.146.61.17`, 2 vCPU / 1.9 GB RAM / 9.7 GB disk), repo in `~/observability`, `docker compose -f docker-compose.prod.yml`.
  Storage: GCS bucket `aoeskills_bucket-1` (S3 XML API + HMAC key), Cloud SQL Postgres on a private IP, user `obs_app` (db-init
  created the three databases itself). 2026-10-15 incl. its own agent: ~1.7 GB RAM used, ~280 MB available (OpenObserve ~410 MB,
  Grafana ~350 MB, collector ~65 MB, otel-agent ~60 MB); disk 92 % (~750 MB free, over the default CRIT 85 %).
- **Repo on the VM**: read-only GitHub deploy key `~/.ssh/observability_deploy` (`git config core.sshCommand` in the repo), so
  `git pull` works there without agent forwarding. Changes go Mac → push → `git pull` on the VM.
- **Gateway**: GCP firewall rule `allow-otel-gateway` = tcp:4317 from 0.0.0.0/0 to network tag `otel-gateway` (owner: the Mac's IP
  changes, the client certificate is the key); `.env` `OTEL_GATEWAY_BIND=0.0.0.0`; certificate SANs `34.146.61.17` + `127.0.0.1`
  (the VM's own agent dials 127.0.0.1:4317), key owned by uid 10001, until 2029-01-16.
- **CA**: on the collector, `~/observability/pki/` (created 2026-10-15; the first CA lived on this Mac and its key was lost
  when the Mac's clone was replaced, so every certificate was re-issued). Back up `pki/ca.key` off the VM (collector runbook step 8).
- **Agents get no clone of this repo** (owner, 2026-10-15): only the files agent runbook step 2 copies, into `~/agent`,
  driven over ssh from the operator's clone; the collector's own agent too (`~/agent`, not the repo's `agent/`).
- **Access**: `ssh -N -L 8080:127.0.0.1:80 GCP_aoeskills` → http://localhost:8080/grafana/, or Tailscale
  http://aoeskills-first-host.tail8656d8.ts.net/grafana/ (`tailscale serve` forwards tailnet :80 to 127.0.0.1:80). Both verified.
- **Passwords** (found while deploying): Cloud SQL requires lower + upper + digit + special, and `PG_PASSWORD` goes into a URL
  (OpenObserve: `InvalidPort` otherwise) → use only `-._~` as specials, e.g. `printf 'Aa1-%s' "$(openssl rand -hex 20)"`. The same
  form satisfies OpenObserve's root password rule (crash-loop "ZO_ROOT_USER_PASSWORD is too weak" otherwise). No `$` in `.env`.
- **LINE**: real delivery verified 2026-10-13. A 401 in Grafana's log ("Notify for alerts failed ... 401") = invalid channel token;
  check a token without sending: `GET https://api.line.me/v2/bot/info` with `Authorization: Bearer <token>` (200 = valid). Test a
  contact point on Grafana 13: `POST /grafana/apis/notifications.alerting.grafana.app/v1beta1/namespaces/default/receivers/<base64url(name)>/test`
  with `{"integration": <the receiver's stored integration>, "alert": {...}}` (the old `/api/alertmanager/.../receivers/test` → 410).
- **This Mac as an agent** (off since 2026-10-15, needs a certificate from the collector first): its local-mode collector is stopped (`docker compose -f docker-compose.local.yml down`, data kept);
  the agent runs alone: `cd agent && docker compose -f docker-compose.yml -f docker-compose.mac.yml up -d` (`agent/.env`:
  `HOST_NAME=local-mac`, `SERVICE_NAMESPACE=sclaser_food`, `GATEWAY_ENDPOINT=34.146.61.17:4317`; probes in
  `agent/probes/sclaser-food.yml`). sclaser-food runs in prod mode on the Mac (`npm run build && npm run start:daemon`, `.env.local`).
- Verified 2026-10-13 end to end: Mac metrics / traces / logs / service probe → gateway → OpenObserve → GCS; Fleet Overview cards;
  Service DOWN pending after stopping sclaser-food, back to normal after restart; Machine DOWN briefly pending once when the
  agent's gRPC connection dropped (EOF, reconnected after 5 s) — watch for repeats (keepalive).
- **Linux agents, verified 2026-10-15** (both with the node-exporter profile): `aoeskills-first-host` (project
  `observability`, gateway 127.0.0.1:4317, no probes) and the sclaser VM `instance-20260804-060650` (`34.104.203.72`, project
  `sclaser_food`, `AGENT_OTLP_BIND=172.17.0.1:4318`, probe `http://127.0.0.1:3000/food/login`; the old stack's node_exporter
  on 0.0.0.0:9100 (`~/monitoring`) is stopped, 9100 is now 127.0.0.1 only). Both `up` + `machine_registered`, the probe
  `service_registered`; sclaser-food's new image (OTel preload) in Docker → traces in stream `sclaser_food` with
  `service_host_name` = the VM; logs path checked with one OTLP test record (`service_name=agent-check`) because the app logs
  only failed requests. Card names = the GCP instance names (on the sclaser VM the hostname is the same, so GCP detection vs
  hostname is not told apart yet).
- Still open: Linux memory panels / alerts (lowercased names, see gotchas); retention actually deleting expired files.

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
  Prod collector: `docker compose -f docker-compose.prod.yml up -d` / `down`. Needs Docker Compose ≥ 2.20 (`include`).
  Why no wrapper script: `include` with `env_file` gives each mode its storage settings, and an including file may extend an
  included service (local adds `depends_on` on its bucket / postgres). Verified 2026-10-13: up, data kept, down removes all.
- Agents run only `agent/` (`docker compose up -d` there); this Mac too since the deploy (see "Deployment").
- Change collector config → `docker compose -f docker-compose.local.yml restart otel-collector`. Change an env file → `docker compose -f docker-compose.local.yml up -d` (recreate).
- Query from a shell: PromQL `GET /openobserve/api/default/prometheus/api/v1/query?query=...`; SQL `POST /openobserve/api/default/_search?type=logs|traces|metrics`
  with `{"query":{"sql":..., "start_time":µs, "end_time":µs}}`. Basic auth with the `.env` credentials.
- In this Claude Code setup the `rtk` command rewriter can mangle `curl`/`grep`/`docker logs` output; use `/usr/bin/curl`,
  `/usr/bin/grep`, `rtk proxy docker logs ...` or a small Python script when the output looks truncated.
