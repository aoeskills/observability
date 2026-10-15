# Grafana reads metrics from OpenObserve's Prometheus API
# (no round(x, 0.1): OpenObserve rejects it), served at the root path, macOS memory supported.
"""Grafana alerting provisioning (contact points, policies, rules), printed to stdout.
provisioner/provision.py writes it to the provisioning volume; ALERTS_MODE picks the mode.

usage: gen_alerts.py prod|test
 - one LINE message the moment a problem starts (and one when it recovers); for usage only CRIT
 - the scheduled digest is not here: provisioner/digest.py sends it, reading the firing alerts of these rules (their
   annotation `digest` is the alert's line in it)
Thresholds come from PostgreSQL via postgres-exporter (usage_threshold_warn / usage_threshold_crit):
a machine's own row wins, otherwise the default row (instance="*").
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from alert_queries import (MACHINE_REGISTERED, MACHINE_UP, RESOURCES, SERVICE_REGISTERED,  # noqa: E402
                           SERVICE_UP, T_CRIT, T_MACHINE, T_SERVICE, T_THRESHOLDS, USAGE_BY, effective, spaced)

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
    T = dict(imm_wait="10s", imm_int="1m", imm_rep="8760h", every="1m", pend="1m", pend_usage="1m", pend_down="1m")
else:
    T = dict(imm_wait="30s", imm_int="5m", imm_rep="8760h", every="1m", pend="1m", pend_usage="2m", pend_down="4m")
    # pend_down (machine and service DOWN): rules are evaluated every 1m and the agents probe services every 1m, so pending
    # 4m = 5 bad evaluations in a row (owner: "every minute, 5 in a row"). A short hiccup does not page the owner.

# DOWN = a machine in the inventory without an up == 1 sample in the last minute (alert_queries.py). 1 minute of silence +
# pending 4m = the owner's "checked every minute, 5 in a row". `+ 0`: see the quirks in alert_queries.py.
MACHINE_DOWN = f'({MACHINE_REGISTERED} + 0) unless ({MACHINE_UP} + 0)'

# Services, the same way (owner, 2026-10-13: machines and services are both pushed, so both use an inventory): DOWN = a
# service in the inventory without a successful probe result in the last 2 minutes, i.e. its probes fail (not HTTP 200) or
# stopped arriving (agent, blackbox-exporter or the machine stopped; a dead machine therefore fires Machine DOWN and Service
# DOWN for each of its services). Both sides keep the same labels (instance, service, service_namespace): OpenObserve's
# `unless` ignores on(...) and compares whole label sets, and a join (group_left) for target would fail with many-to-many
# while a probe's URL changes.
SERVICE_DOWN = f'({SERVICE_REGISTERED} + 0) unless ({SERVICE_UP} + 0)'

CRIT_BREACH = f'{USAGE_BY} >= on(instance,resource) {effective("crit")}'

# Message texts (owner, 2026-10-14): state only what the stack can confirm (how long something has not answered), never
# guessed causes; finding the cause is the owner's job. The thresholds alert explains what it is, since its data comes from
# the collector's own database, not from an agent. Each rule has a firing text (summary), a recovery title + text (only
# labels: on recovery Grafana sends the annotations of the last firing evaluation) and one line for the digest.


def by_resource(values, default):
    """template snippet: values[resource label], or default for a resource not in the table"""
    out = "".join(f'{{{{ {"if" if i == 0 else "else if"} eq $labels.resource "{r}" }}}}{v}' for i, (r, v) in enumerate(values.items()))
    return out + f"{{{{ else }}}}{default}{{{{ end }}}}"


RES_NAME = by_resource({r: spaced(n, False) for r, (n, _) in RESOURCES.items()}, " {{ $labels.resource }}")  # 「的 CPU」
RES_NAME_ = by_resource({r: spaced(n, True) for r, (n, _) in RESOURCES.items()}, " {{ $labels.resource }} ")  # 「的 CPU 用量」
RES_UNIT = by_resource({r: u for r, (_, u) in RESOURCES.items()}, "")


def minutes(duration):
    return int(duration.rstrip("m")) if duration.endswith("m") else 0


DOWN_MIN = 1 + minutes(T["pend_down"])  # 1 minute without data, then pending: "every minute, 5 in a row"
MACHINE = "{{ $labels.instance }}（專案 {{ $labels.service_namespace }}）"
SERVICE = "{{ $labels.service }}（在 {{ $labels.instance }}，專案 {{ $labels.service_namespace }}）"


def prom_q(ref, expr):
    return {"refId": ref, "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": PROM_UID,
            "model": {"refId": ref, "datasource": PROM, "expr": expr, "instant": True, "range": False,
                      "intervalMs": 1000, "maxDataPoints": 43200}}


def expr(ref, model):
    return {"refId": ref, "relativeTimeRange": {"from": 0, "to": 0}, "datasourceUid": "__expr__",
            "model": dict(model, refId=ref, datasource={"type": "__expr__", "uid": "__expr__"})}


def reduce_(ref, src, reducer="last"):
    return expr(ref, {"type": "reduce", "expression": src, "reducer": reducer, "settings": {"mode": "dropNN"}})


def math(ref, expression):
    return expr(ref, {"type": "math", "expression": expression})


def thresh(ref, src, op, val):
    return expr(ref, {"type": "threshold", "expression": src,
                      "conditions": [{"evaluator": {"type": op, "params": [val]}}]})


def rule(uid, title, cond, data, summary, resolved_title, resolved, digest, pend=None):
    annotations = {"summary": summary, "resolved_title": resolved_title, "resolved": resolved, "digest": digest}
    return {"uid": uid, "title": title, "condition": cond, "data": data, "for": pend or T["pend"],
            "noDataState": "OK", "execErrState": "Alerting", "isPaused": False,
            # digest: no route uses it since 2026-10-15; kept because a label change makes every firing alert a new one
            # (one "recovered" + one "firing" message each at deploy)
            "labels": {"kind": "problem", "digest": "all", "severity": "critical"}, "annotations": annotations}


rules = [
    rule("machine-down", T_MACHINE, "C",
         [prom_q("A", MACHINE_DOWN), reduce_("B", "A"), thresh("C", "B", "gt", 0)],
         f"{MACHINE}超過 {DOWN_MIN} 分鐘沒有回應，已經不用的機器，在 Fleet Overview 它的卡片上按 Retire。",
         "機器已恢復 Machine UP", f"{MACHINE}已經恢復", f"🔴 機器沒有回應：{MACHINE}", pend=T["pend_down"]),
    # Services: probed by the agent on their own machine (agent/probes/*.yml) and pushed like everything else, so `instance`
    # is the machine that probes it. See SERVICE_DOWN.
    rule("service-down", T_SERVICE, "C",
         [prom_q("A", SERVICE_DOWN), reduce_("B", "A"), thresh("C", "B", "gt", 0)],
         f"{SERVICE}超過 {DOWN_MIN} 分鐘沒有回應，已經不用的服務，在 Fleet Overview 它的卡片上按 Retire。",
         "服務已恢復 Service UP", f"{SERVICE}已經恢復", f"🔴 服務沒有回應：{SERVICE}", pend=T["pend_down"]),
    # Usage: only CRIT is sent at once (owner, 2026-10-14); WARN shows up in the digest's 12-hour report (digest.py).
    rule("usage-crit", T_CRIT, "C",
         [prom_q("A", CRIT_BREACH), reduce_("B", "A"), thresh("C", "B", "gt", -1),
          prom_q("D", effective("crit")), reduce_("E", "D")],
         f'{{{{ $labels.instance }}}} 的{RES_NAME_}用量 {{{{ printf "%.1f" $values.B.Value }}}}{RES_UNIT}，'
         f'超過嚴重門檻({{{{ printf "%g" $values.E.Value }}}}{RES_UNIT}) 已持續 {minutes(T["pend_usage"])} 分鐘。',
         "用量回到嚴重門檻以下 Usage below CRIT", f"{{{{ $labels.instance }}}} 的{RES_NAME_}用量已回到嚴重門檻以下",
         f'🔴 用量超過嚴重門檻：{{{{ $labels.instance }}}} 的{RES_NAME} {{{{ printf "%.1f" $values.B.Value }}}}{RES_UNIT}',
         pend=T["pend_usage"]),
    rule("thresholds-missing", T_THRESHOLDS, "C",
         [prom_q("A", 'absent(usage_threshold_warn{instance="*"})'), reduce_("B", "A"), thresh("C", "B", "gt", 0)],
         f"超過 {minutes(T['pend'])} 分鐘讀不到警告門檻（資料庫或 postgres-exporter 可能掛了），用量超標的告警目前不會觸發。",
         "Grafana 可以讀到用量警告門檻資料 Thresholds available", "用量超標的告警恢復運作。",
         "🔴 Grafana 讀不到用量警告門檻資料"),
]

# rules that left this file (file provisioning never deletes them on its own; a uid that does not exist is ignored)
REMOVED_RULES = ["usage-warn",  # 2026-10-14: WARN only in the digest's 12-hour report
                 # 2026-10-15: the digest moved to provisioner/digest.py (it computes the 12-hour report itself)
                 "heartbeat", "machine-downtime", "service-downtime", "usage-12h"]
# contact points, mute timings and templates of the digest, also gone since 2026-10-15
REMOVED_CONTACT_POINTS = ["line-push-startup", "line-push-digest"]
REMOVED_MUTE_TIMES = ["digest-times", "startup-window"]
REMOVED_TEMPLATES = ["digest"]  # its LINE templates are now in template "line"

TEMPLATE = r"""{{ define "line.text.immediate" -}}
{{- if eq .Status "firing" }}🔴 告警{{ else }}✅ 已恢復{{ end }}
{{- range .Alerts }}
{{ if eq .Status "firing" }}{{ .Labels.alertname }}
{{ .Annotations.summary }}{{ else }}{{ .Annotations.resolved_title }}
{{ .Annotations.resolved }}{{ end }}
{{- end }}
{{- end }}

{{ define "line.payload.immediate" -}}
{{ coll.Dict "to" .Vars.to "messages" (coll.Slice (coll.Dict "type" "text" "text" (tmpl.Exec "line.text.immediate" .))) | data.ToJSON }}
{{- end }}
"""


def line_receiver(uid, payload_template):
    # NOTE: authorization_credentials must be in "settings" (Grafana encrypts it itself). In "secureSettings" the
    # provisioned webhook sends NO Authorization header (tested on Grafana 13.2.3). The value is an env var, not a secret in the file.
    return {"uid": uid, "type": "webhook",
            "settings": {"url": LINE_URL, "httpMethod": "POST", "authorization_scheme": "Bearer",
                         "authorization_credentials": LINE_TOKEN,
                         "payload": {"template": '{{ template "%s" . }}' % payload_template, "vars": {"to": LINE_TO}}}}


doc = {
    "apiVersion": 1,
    "groups": [{"orgId": 1, "name": "monitoring", "folder": "Alerts", "interval": T["every"], "rules": rules}],
    "deleteRules": [{"orgId": 1, "uid": uid} for uid in REMOVED_RULES],
    "deleteContactPoints": [{"orgId": 1, "uid": uid} for uid in REMOVED_CONTACT_POINTS],
    "deleteMuteTimes": [{"orgId": 1, "name": name} for name in REMOVED_MUTE_TIMES],
    "deleteTemplates": [{"orgId": 1, "name": name} for name in REMOVED_TEMPLATES],
    "contactPoints": [
        {"orgId": 1, "name": "line-push-immediate", "receivers": [line_receiver("line-push-immediate", "line.payload.immediate")]}],
    "policies": [{"orgId": 1, "receiver": "line-push-immediate", "group_by": ["grafana_folder", "alertname"], "routes": [
        {"receiver": "line-push-immediate", "object_matchers": [["kind", "=", "problem"]], "group_by": ["..."],
         "group_wait": T["imm_wait"], "group_interval": T["imm_int"], "repeat_interval": T["imm_rep"]}]}],
    "templates": [{"orgId": 1, "name": "line", "template": TEMPLATE}],
}

print(f"# Alerting (generated for mode={MODE}). Edit timers / texts in the generator, not by hand.")
print(json.dumps(doc, indent=2, ensure_ascii=False))
