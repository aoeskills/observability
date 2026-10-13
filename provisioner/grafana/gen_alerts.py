# Adapted from the GCP ~/monitoring generator for this stack: Grafana reads metrics from OpenObserve's Prometheus API
# (no round(x, 0.1): OpenObserve rejects it), served at the root path, macOS memory supported.
"""Grafana alerting provisioning (contact points, policies, rules), printed to stdout.
provisioner/provision.py writes it to the provisioning volume; ALERTS_MODE picks the mode.

usage: gen_alerts.py prod|test
 - one LINE message the moment a problem starts (and one when it recovers)
 - a digest message when the stack starts and at DIGEST_TIMES (05:00 / 17:00 Asia/Taipei) that ALWAYS goes out: lists
   what is wrong, or says everything is fine (a heartbeat alert that is always firing keeps the digest groups alive)
Thresholds come from PostgreSQL via postgres-exporter (usage_threshold_warn / usage_threshold_crit):
a machine's own row wins, otherwise the default row (instance="*").
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

MODE = sys.argv[1] if len(sys.argv) > 1 else "prod"
PROM_UID = "openobserve"  # grafana/provisioning/datasources/openobserve.yml
PROM = {"type": "prometheus", "uid": PROM_UID}

# LINE Messaging API "push" (the owner chose LINE; the token and user id are NOT in any file: Grafana reads them from its env, see .env)
if MODE == "test":
    LINE_URL, LINE_TOKEN, LINE_TO = "http://fake-line:8080/v2/bot/message/push", "TESTTOKEN", "Utest"  # tools/fake_line_server.py
else:
    # LINE_PUSH_URL (generate time) points prod-mode rules at tools/fake_line_server.py for a test with real env credentials
    LINE_URL = os.environ.get("LINE_PUSH_URL") or "https://api.line.me/v2/bot/message/push"
    LINE_TOKEN, LINE_TO = "$LINE_CHANNEL_ACCESS_TOKEN", "$LINE_USER_ID"

if MODE == "test":   # short timers so a test run finishes in a few minutes
    T = dict(imm_wait="10s", imm_int="1m", imm_rep="8760h", start_wait="20s", every="1m", pend="1m", pend_usage="1m", pend_down="1m")
else:
    T = dict(imm_wait="30s", imm_int="5m", imm_rep="8760h", start_wait="1m", every="1m", pend="1m", pend_usage="2m", pend_down="4m")
    # pend_down (machine and service DOWN): rules are evaluated every 1m and the agents probe services every 1m, so pending
    # 4m = 5 bad evaluations in a row (owner: "every minute, 5 in a row"). A short hiccup does not page the owner.

# Scheduled digest (owner, 2026-10-13: 05:00 and 17:00 UTC+8). Alertmanager has no cron: the digest group flushes every
# minute, but only inside a 2-minute active time interval at each time; repeat 1h > the window, so one message per window
# (another one only if the alerts change within those 2 minutes). Muted flushes are not logged, so the first one in the
# window always sends.
DIGEST_TZ = "Asia/Taipei"
DIGEST_TIMES = [t.strip() for t in os.environ.get("DIGEST_TIMES", "05:00,17:00").split(",") if t.strip()]
# Startup digest: Grafana keeps its notification log in Postgres, so after a restart the same firing alerts would count as
# "already sent". The heartbeat carries the provisioner's start time (one value per provisioner process, see provision.py),
# which makes every start a new alert set. The startup route never repeats (group_interval / repeat 8760h).
STARTED = os.environ.get("PROVISIONER_STARTED") or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def hhmm_plus(t, minutes):
    h, m = map(int, t.split(":"))
    total = min(h * 60 + m + minutes, 24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


CPU = 'round((100 - avg by(instance,job,service_namespace)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) * 10) / 10'
# Linux MemAvailable, else macOS (active + wired + compressed) / total
MEM = ('round((100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
       ' or 100 * (node_memory_active_bytes + node_memory_wired_bytes + node_memory_compressed_bytes) / node_memory_total_bytes) * 10) / 10')
DSEL = 'mountpoint="/",fstype!~"tmpfs|rootfs|overlay"'
DISK = (f'round((100 - 100 * max by(instance,job,service_namespace)(node_filesystem_avail_bytes{{{DSEL}}})'
        f' / max by(instance,job,service_namespace)(node_filesystem_size_bytes{{{DSEL}}})) * 10) / 10')
USAGE = "(" + " or ".join(f'label_replace({e}, "resource", "{r}", "", "")'
                          for e, r in ((CPU, "cpu"), (MEM, "memory"), (DISK, "disk"))) + ")"


# OpenObserve (v1.0.4) quirks, see AGENTS.md "OpenObserve gotchas":
#  - `or` / `unless` only match series whose whole label sets are equal (on(...) is not enough once one side went through
#    label_replace), so both sides are reduced to {instance, resource} first;
#  - a bare selector / aggregation that returns nothing makes `x or <it>` / `x unless <it>` empty, so such operands get
#    `+ 0` (no machine has its own thresholds = the normal case; before this, usage alerts then never fired).
USAGE_BY = f'max by(instance,resource)({USAGE})'


def effective(level):
    """threshold per (instance, resource): the machine's own row, else the default row expanded to every machine"""
    own = f'(max by(instance,resource)(usage_threshold_{level}{{instance!="*"}}) + 0)'  # instance = host.name (exporter)
    default = f'usage_threshold_{level}{{instance="*"}}'
    expanded = f'(max by(instance,resource)({USAGE} * 0 + on(resource) group_left() {default}) + 0)'
    return f'({own} or {expanded})'


# Agents push, so a dead machine (or agent) is silence, not up = 0. DOWN = a machine in the inventory (table machine, every
# machine ever seen until retired; exported as machine_registered) without an up == 1 sample in the last minute.
# 1 minute of silence + pending 4m = the owner's "checked every minute, 5 in a row". `+ 0`: see the quirks above.
# machine_registered also over the last minute: a retired machine stops being exported, and without [1m] its last sample
# would count for 5 more minutes (remote write has no staleness markers).
MACHINE_DOWN = ('(max by(instance,service_namespace)(max_over_time(machine_registered[1m])) + 0) unless '
                '(max by(instance,service_namespace)(max_over_time(up{job="node"}[1m]) == 1) + 0)')

CRIT_BREACH = f'{USAGE_BY} >= on(instance,resource) {effective("crit")}'
WARN_BREACH = (f'({USAGE_BY} >= on(instance,resource) {effective("warn")})'
               f' unless ({USAGE_BY} >= on(instance,resource) {effective("crit")})')


def prom_q(ref, expr):
    return {"refId": ref, "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": PROM_UID,
            "model": {"refId": ref, "datasource": PROM, "expr": expr, "instant": True, "range": False,
                      "intervalMs": 1000, "maxDataPoints": 43200}}


def expr(ref, model):
    return {"refId": ref, "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
            "model": dict(model, refId=ref, datasource={"type": "__expr__", "uid": "__expr__"})}


def reduce_(ref, src):
    return expr(ref, {"type": "reduce", "expression": src, "reducer": "last", "settings": {"mode": "dropNN"}})


def thresh(ref, src, op, val):
    return expr(ref, {"type": "threshold", "expression": src,
                      "conditions": [{"evaluator": {"type": op, "params": [val]}}]})


def rule(uid, title, cond, data, summary, kind="problem", severity="critical", pend=None, no_data="OK", labels=None):
    return {"uid": uid, "title": title, "condition": cond, "data": data, "for": pend or T["pend"],
            "noDataState": no_data, "execErrState": "Alerting", "isPaused": False,
            "labels": {"kind": kind, "digest": "all", "severity": severity, **(labels or {})},
            "annotations": {"summary": summary}}


def usage_rule(uid, title, promql, level_text, severity):
    return rule(uid, title, "C", [prom_q("A", promql), reduce_("B", "A"), thresh("C", "B", "gt", -1)],
                '{{ $labels.instance }} {{ $labels.resource }} 用量 {{ printf "%.1f" $values.B.Value }}%，超過' + level_text + "門檻",
                severity=severity, pend=T["pend_usage"])


rules = [
    rule("heartbeat", "心跳 Heartbeat（監控運作中）", "B",
         [expr("A", {"type": "math", "expression": "1"}), thresh("B", "A", "gt", 0)],
         "監控系統運作中。這一行永遠存在，目的是讓每天 05:00 / 17:00 的彙整一定會送出；沒收到彙整就代表監控本身壞了。",
         kind="heartbeat", severity="none", pend="0s", labels={"started": STARTED}),
    rule("machine-down", "機器沒有回應 Machine DOWN", "C",
         [prom_q("A", MACHINE_DOWN), reduce_("B", "A"), thresh("C", "B", "gt", 0)],
         "{{ $labels.instance }}（專案 {{ $labels.service_namespace }}）沒有回應：超過 1 分鐘沒收到它的狀態（agent、node_exporter"
         " 或整台機器停了）。已經不用的機器，在 Fleet Overview 它的卡片上按 Retire。", pend=T["pend_down"]),
    # Services: probed by the agent on their own machine (agent/probes/*.yml) and pushed like everything else, so `instance`
    # is the machine and `target` the probed URL. A dead machine stops sending probes too: then only Machine DOWN fires.
    rule("service-down", "服務沒有回應 Service DOWN", "C",
         [prom_q("A", 'probe_success{job="blackbox"}'), reduce_("B", "A"), thresh("C", "B", "lt", 1)],
         "{{ $labels.service }}（在 {{ $labels.instance }}，專案 {{ $labels.service_namespace }}）沒有回應：{{ $labels.target }}"
         " 連續 5 次不是 HTTP 200", pend=T["pend_down"]),
    usage_rule("usage-warn", "用量超過警告門檻 Usage over WARN", WARN_BREACH, "警告", "warning"),
    usage_rule("usage-crit", "用量超過嚴重門檻 Usage over CRIT", CRIT_BREACH, "嚴重", "critical"),
    rule("thresholds-missing", "門檻資料讀不到 Thresholds unavailable", "C",
         [prom_q("A", 'absent(usage_threshold_warn{instance="*"})'), reduce_("B", "A"), thresh("C", "B", "gt", 0)],
         "讀不到預設門檻（資料庫或 postgres-exporter 可能掛了），用量超標的告警目前不會觸發"),
]

TEMPLATE = r"""{{ define "digest.subject" -}}
{{- $bad := 0 -}}
{{- range .Alerts.Firing }}{{ if ne .Labels.kind "heartbeat" }}{{ $bad = 1 }}{{ end }}{{ end -}}
{{- if eq $bad 1 }}[監控] {{ .Vars.title }}：有問題 ⚠️{{ else }}[監控] {{ .Vars.title }}：全部正常 ✅{{ end -}}
{{ end }}

{{ define "digest.message" -}}
{{- $bad := 0 -}}
{{- range .Alerts.Firing }}{{ if ne .Labels.kind "heartbeat" }}{{ $bad = 1 }}{{ end }}{{ end -}}
{{- if eq $bad 0 }}目前所有機器與服務都正常：沒有失去回應，也沒有任何用量超過門檻。{{ else -}}
目前有問題的項目：{{ range .Alerts.Firing }}{{ if ne .Labels.kind "heartbeat" }}
 - {{ .Labels.alertname }}: {{ .Annotations.summary }}{{ end }}{{ end }}{{ end }}
{{ end }}

{{ define "line.text.immediate" -}}
{{- if eq .Status "firing" }}🔴 告警{{ else }}✅ 已恢復{{ end }}
{{- range .Alerts }}
{{ .Labels.alertname }}
{{ .Annotations.summary }}
{{- end }}
{{- end }}

{{ define "line.text.digest" -}}
{{ template "digest.subject" . }}
{{ template "digest.message" . }}
{{- end }}

{{ define "line.payload.immediate" -}}
{{ coll.Dict "to" .Vars.to "messages" (coll.Slice (coll.Dict "type" "text" "text" (tmpl.Exec "line.text.immediate" .))) | data.ToJSON }}
{{- end }}

{{ define "line.payload.digest" -}}
{{ coll.Dict "to" .Vars.to "messages" (coll.Slice (coll.Dict "type" "text" "text" (tmpl.Exec "line.text.digest" .))) | data.ToJSON }}
{{- end }}
"""


def line_receiver(uid, payload_template, **variables):
    # NOTE: authorization_credentials must be in "settings" (Grafana encrypts it itself). In "secureSettings" the
    # provisioned webhook sends NO Authorization header (tested on Grafana 13.2.3). The value is an env var, not a secret in the file.
    return {"uid": uid, "type": "webhook",
            "settings": {"url": LINE_URL, "httpMethod": "POST", "authorization_scheme": "Bearer",
                         "authorization_credentials": LINE_TOKEN,
                         "payload": {"template": '{{ template "%s" . }}' % payload_template, "vars": {"to": LINE_TO, **variables}}}}


def route(receiver, matchers, group_by, wait, interval, repeat, cont, **extra):
    return {"receiver": receiver, "object_matchers": matchers, "group_by": group_by, "group_wait": wait,
            "group_interval": interval, "repeat_interval": repeat, "continue": cont, **extra}


doc = {
    "apiVersion": 1,
    "groups": [{"orgId": 1, "name": "monitoring", "folder": "Alerts", "interval": T["every"], "rules": rules}],
    "contactPoints": [
        {"orgId": 1, "name": "line-push-immediate", "receivers": [line_receiver("line-push-immediate", "line.payload.immediate")]},
        {"orgId": 1, "name": "line-push-startup", "receivers": [
            line_receiver("line-push-startup", "line.payload.digest", title=f"監控啟動 {STARTED}")]},
        {"orgId": 1, "name": "line-push-digest", "receivers": [
            line_receiver("line-push-digest", "line.payload.digest", title="定時彙整")]},
    ],
    "muteTimes": [{"orgId": 1, "name": "digest-times", "time_intervals": [
        {"times": [{"start_time": t, "end_time": hhmm_plus(t, 2)} for t in DIGEST_TIMES], "location": DIGEST_TZ}]}],
    "policies": [{"orgId": 1, "receiver": "line-push-immediate", "group_by": ["grafana_folder", "alertname"], "routes": [
        route("line-push-immediate", [["kind", "=", "problem"]], ["..."], T["imm_wait"], T["imm_int"], T["imm_rep"], True),
        route("line-push-startup", [["digest", "=", "all"]], ["digest"], T["start_wait"], "8760h", "8760h", True),
        route("line-push-digest", [["digest", "=", "all"]], ["digest"], "1m", "1m", "1h", False,
              active_time_intervals=["digest-times"])]}],
    "templates": [{"orgId": 1, "name": "digest", "template": TEMPLATE}],
}

print(f"# Alerting (generated for mode={MODE}). Edit timers / texts in the generator, not by hand.")
print(json.dumps(doc, indent=2, ensure_ascii=False))
