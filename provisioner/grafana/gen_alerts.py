# Grafana reads metrics from OpenObserve's Prometheus API
# (no round(x, 0.1): OpenObserve rejects it), served at the root path, macOS memory supported.
"""Grafana alerting provisioning (contact points, policies, rules), printed to stdout.
provisioner/provision.py writes it to the provisioning volume; ALERTS_MODE picks the mode.

usage: gen_alerts.py prod|test
 - one LINE message the moment a problem starts (and one when it recovers); for usage only CRIT
 - a digest message 3 minutes after the stack starts and at DIGEST_TIMES (05:00 / 17:00 Asia/Taipei) that ALWAYS goes
   out: what is wrong now, or that everything is fine (a heartbeat alert that is always firing keeps the digest groups
   alive), then the last 12 hours: machine / service downtime and minutes over the usage thresholds (WARN lives here)
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
    T = dict(imm_wait="10s", imm_int="1m", imm_rep="8760h", every="1m", pend="1m", pend_usage="1m", pend_down="1m")
else:
    T = dict(imm_wait="30s", imm_int="5m", imm_rep="8760h", every="1m", pend="1m", pend_usage="2m", pend_down="4m")
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
# which makes every start a new alert set.
STARTED = os.environ.get("PROVISIONER_STARTED") or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
# The startup digest is sent in a 2-minute window 3 minutes after the start, the same way as the scheduled one. Not with
# group_wait: Grafana evaluates a group's rules one after another, and Alertmanager flushes a new group at once when an alert
# that started more than group_wait ago arrives (after a restart: every alert still firing, and the previous start's
# heartbeat). The digest went out before the slower 12-hour report rules had been evaluated (seen 2026-10-14).
_start = datetime.strptime(STARTED, "%Y-%m-%d %H:%M") + timedelta(minutes=3)
if _start.date() != (_start + timedelta(minutes=2)).date():  # no window across midnight
    _start = datetime.combine(_start.date() + timedelta(days=1), datetime.min.time())
STARTUP_WINDOW = {"times": [{"start_time": _start.strftime("%H:%M"), "end_time": (_start + timedelta(minutes=2)).strftime("%H:%M")}],
                  "days_of_month": [str(_start.day)], "months": [str(_start.month)], "years": [str(_start.year)],
                  "location": DIGEST_TZ}


def hhmm_plus(t, minutes):
    h, m = map(int, t.split(":"))
    total = min(h * 60 + m + minutes, 24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


# idle time / all CPU time over the same samples: see gen_fleet_overview.py CPU (OpenObserve's rate() over a partly empty
# window made every agent start / stop look like 80-90 % CPU and fired Usage over CRIT)
CPU_BY = "instance,job,service_namespace"
CPU = (f'round(100 * (1 - sum by({CPU_BY})(rate(node_cpu_seconds_total{{mode="idle"}}[5m]))'
       f' / sum by({CPU_BY})(rate(node_cpu_seconds_total[5m]))) * 10) / 10')
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

# Services, the same way (owner, 2026-10-13: machines and services are both pushed, so both use an inventory): DOWN = a
# service in the inventory (table service, exported as service_registered) without a successful probe result in the last
# 2 minutes, i.e. its probes fail (not HTTP 200) or stopped arriving (agent, blackbox-exporter or the machine stopped; a
# dead machine therefore fires Machine DOWN and Service DOWN for each of its services). [2m]: the agent probes once a
# minute, so a [1m] window would sometimes hold no sample at all. last_over_time: the newest result counts, a failure is
# not hidden by an older success. Both sides keep the same labels (instance, service, service_namespace): OpenObserve's
# `unless` ignores on(...) and compares whole label sets (see the quirks above), and a join (group_left) for target
# would fail with many-to-many while a probe's URL changes.
SVC_KEY = "instance,service,service_namespace"
SERVICE_DOWN = (f'(max by({SVC_KEY})(max_over_time(service_registered[1m])) + 0) unless '
                f'(max by({SVC_KEY})(last_over_time(probe_success{{job="blackbox"}}[2m]) == 1) + 0)')

CRIT_BREACH = f'{USAGE_BY} >= on(instance,resource) {effective("crit")}'
# 1 in a minute over the threshold, else 0, for the 12-hour report: a query without any series (no minute over crit) would
# make Grafana treat the whole rule as NoData
OVER_WARN = f'{USAGE_BY} >= bool on(instance,resource) {effective("warn")}'
OVER_CRIT = f'{USAGE_BY} >= bool on(instance,resource) {effective("crit")}'

# Message texts (owner, 2026-10-14): state only what the stack can confirm (how long something has not answered), never
# guessed causes; finding the cause is the owner's job. The thresholds alert explains what it is, since its data comes from
# the collector's own database, not from an agent. Each rule has a firing text (summary), a recovery title + text (only
# labels: on recovery Grafana sends the annotations of the last firing evaluation) and one line for the digest.

# How resources are named and measured in the texts. A new usage kind (e.g. network traffic in GB/m) is one more entry.
RESOURCES = {"cpu": ("CPU", "%"), "memory": ("記憶體", "%"), "disk": ("磁碟", "%")}


def by_resource(values, default):
    """template snippet: values[resource label], or default for a resource not in the table"""
    out = "".join(f'{{{{ {"if" if i == 0 else "else if"} eq $labels.resource "{r}" }}}}{v}' for i, (r, v) in enumerate(values.items()))
    return out + f"{{{{ else }}}}{default}{{{{ end }}}}"


def spaced(name, after):
    """a space between a Latin name and the Chinese around it: 「的 CPU 用量」, 「的磁碟用量」"""
    latin = name[0].isascii()
    return (" " if latin else "") + name + (" " if latin and after else "")


RES_NAME = by_resource({r: spaced(n, False) for r, (n, _) in RESOURCES.items()}, " {{ $labels.resource }}")  # 「的 CPU：」
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


# The 12-hour report: one point per minute over the window, evaluated by Grafana as a range query and reduced by Grafana.
# Not PromQL subqueries / set operators: in OpenObserve v1.0.4 `x[12h:1m]` evaluates x once (count_over_time(...[1h:1m])
# = 1), and in a range query `unless` / `or` act on whole series, not per step (`registered unless up` returned nothing
# while 8 of 112 minutes had no up). Counting points per series works, so downtime = minutes registered - minutes up.
REPORT_MIN = 12 * 60


def prom_range(ref, expr):
    return {"refId": ref, "relativeTimeRange": {"from": REPORT_MIN * 60, "to": 0}, "datasourceUid": PROM_UID,
            "model": {"refId": ref, "datasource": PROM, "expr": expr, "instant": False, "range": True,
                      "interval": "1m", "intervalMs": 60000, "maxDataPoints": REPORT_MIN}}


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


def rule(uid, title, cond, data, summary, resolved_title="", resolved="", digest="", kind="problem", severity="critical",
         pend=None, no_data="OK", exec_err="Alerting", labels=None):
    annotations = {"summary": summary, "resolved_title": resolved_title, "resolved": resolved, "digest": digest}
    return {"uid": uid, "title": title, "condition": cond, "data": data, "for": pend or T["pend"],
            "noDataState": no_data, "execErrState": exec_err, "isPaused": False,
            "labels": {"kind": kind, "digest": "all", "severity": severity, **(labels or {})},
            "annotations": {k: v for k, v in annotations.items() if v}}


def report_rule(uid, title, cond, data, digest):
    # kind=report: only the digest routes match it, never the immediate one (kind=problem). execErrState OK: a failing
    # 12-hour query leaves its section out of the digest instead of adding a line without labels.
    return rule(uid, title, cond, data, "", digest=digest, kind="report", severity="none", pend="0s", exec_err="OK")


def downtime_rule(uid, title, registered, up, what):
    """minutes in the last 12 hours that a still registered machine / service was registered without being up"""
    return report_rule(uid, title, "I", [
        prom_range("A", registered), reduce_("B", "A", "count"),         # minutes registered
        prom_range("C", f"({up}) or ({registered} * 0)"), reduce_("D", "C", "sum"),  # minutes up (0 if never up)
        prom_q("E", registered), reduce_("F", "E"),                      # still registered now (retired: no series)
        math("G", "($B - $D) * $F"), math("H", f"100 - $G * 100 / {REPORT_MIN}"), thresh("I", "G", "gt", 0)],
        what + '：停機 {{ printf "%.0f" $values.G.Value }} 分鐘，可用率 {{ printf "%.1f" $values.H.Value }}%')


T_MACHINE, T_SERVICE = "機器沒有回應 Machine DOWN", "服務沒有回應 Service DOWN"
T_CRIT, T_THRESHOLDS = "用量超過嚴重門檻 Usage over CRIT", "Grafana 讀不到用量警告門檻資料 Thresholds unavailable"
R_MACHINE, R_SERVICE = "機器停機（過去 12 小時）Machine downtime 12h", "服務停機（過去 12 小時）Service downtime 12h"
R_USAGE = "用量超標（過去 12 小時）Usage over WARN 12h"

rules = [
    rule("heartbeat", "心跳 Heartbeat（監控運作中）", "B",
         [expr("A", {"type": "math", "expression": "1"}), thresh("B", "A", "gt", 0)],
         "監控系統運作中。這一行永遠存在，目的是讓每天 05:00 / 17:00 的彙整一定會送出；沒收到彙整就代表監控本身壞了。",
         kind="heartbeat", severity="none", pend="0s", labels={"started": STARTED}),
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
    # Usage: only CRIT is sent at once (owner, 2026-10-14); WARN shows up in the digest's 12-hour report (usage-12h).
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
    downtime_rule("machine-downtime", R_MACHINE, "max by(instance,service_namespace)(max_over_time(machine_registered[1m]))",
                  'max by(instance,service_namespace)(max_over_time(up{job="node"}[1m]) == 1)', MACHINE),
    downtime_rule("service-downtime", R_SERVICE, f"max by({SVC_KEY})(max_over_time(service_registered[1m]))",
                  f'max by({SVC_KEY})(last_over_time(probe_success{{job="blackbox"}}[2m]) == 1)', SERVICE),
    # minutes over the warn / crit threshold (each is counted against the threshold of that minute; the text shows the
    # latest), the highest value, the latest thresholds: range queries too, since effective() only has series while the
    # machine sends usage, and a machine that stopped is still reported
    report_rule("usage-12h", R_USAGE, "K", [
        prom_range("A", OVER_WARN), reduce_("B", "A", "sum"),
        prom_range("C", OVER_CRIT), reduce_("D", "C", "sum"),
        prom_range("E", USAGE_BY), reduce_("F", "E", "max"),
        prom_range("G", effective("warn")), reduce_("H", "G"),
        prom_range("I", effective("crit")), reduce_("J", "I"),
        thresh("K", "B", "gt", 0)],
        f'{{{{ $labels.instance }}}} 的{RES_NAME}：超過警告門檻({{{{ printf "%g" $values.H.Value }}}}{RES_UNIT}) '
        f'{{{{ printf "%.0f" $values.B.Value }}}} 分鐘{{{{ with $values.D }}}}{{{{ if gt .Value 0.0 }}}}，'
        f'其中嚴重({{{{ printf "%g" $values.J.Value }}}}{RES_UNIT}) {{{{ printf "%.0f" .Value }}}} 分鐘{{{{ end }}}}{{{{ end }}}}，'
        f'最高 {{{{ printf "%.1f" $values.F.Value }}}}{RES_UNIT}'),
]

REMOVED_RULES = ["usage-warn"]  # 2026-10-14: WARN only in the digest (usage-12h)

# Digest sections, each a list of rule titles in display order; at most DIGEST_MAX lines each (LINE: 5000 characters per
# message, a longer one is refused as a whole).
DIGEST_MAX = 20
NOW_SECTION = [T_MACHINE, T_SERVICE, T_CRIT, T_THRESHOLDS]
REPORT_SECTIONS = [("機器", R_MACHINE), ("服務", R_SERVICE), ("用量超標", R_USAGE)]


def lines_of(var, titles):
    """template: collect the digest lines of the firing alerts of these rules, in this order, into slice $var"""
    return f"{{{{- ${var} := coll.Slice -}}}}" + "".join(
        f'{{{{- range $.Alerts.Firing }}}}{{{{ if eq .Labels.alertname "{t}" }}}}'
        f'{{{{ ${var} = coll.Append .Annotations.digest ${var} }}}}{{{{ end }}}}{{{{ end -}}}}' for t in titles)


def print_lines(var):
    return (f'{{{{- range $i, $l := ${var} }}}}{{{{ if lt $i {DIGEST_MAX} }}}}\n - {{{{ $l }}}}{{{{ end }}}}{{{{ end -}}}}'
            f'{{{{- if gt (len ${var}) {DIGEST_MAX} }}}}\n （共 {{{{ len ${var} }}}} 項，只列前 {DIGEST_MAX} 項）{{{{ end -}}}}')


# Every newline of the message is written inside an action ({{- ... -}} trims the template's own line breaks).
DIGEST_MESSAGE = (
    '{{ define "digest.message" -}}\n'
    + lines_of("now", NOW_SECTION)
    + "".join(lines_of(f"r{i}", [t]) for i, (_, t) in enumerate(REPORT_SECTIONS))
    + '{{- if eq (len $now) 0 }}目前所有機器與服務都正常。{{ else }}目前有問題：' + print_lines("now") + "{{ end -}}\n"
    + "{{- if or " + " ".join(f"(len $r{i})" for i in range(len(REPORT_SECTIONS))) + " }}\n\n📋 過去 12 小時彙整{{ end -}}\n"
    + '{{- $gap := "" -}}\n'  # a blank line between sections, not after the heading
    + "".join(f"{{{{- if len $r{i} }}}}{{{{ $gap }}}}\n【{name}】" + print_lines(f"r{i}") + '{{ $gap = "\\n" }}{{ end -}}\n'
              for i, (name, _) in enumerate(REPORT_SECTIONS))
    + "{{- if not (or " + " ".join(f"(len $r{i})" for i in range(len(REPORT_SECTIONS)))
    + ") }}\n\n📋 過去 12 小時：沒有停機，用量也沒有超標。{{ end -}}\n"
    + "{{ end }}\n")

TEMPLATE = r"""{{ define "digest.subject" -}}
{{- $bad := 0 -}}
{{- range .Alerts.Firing }}{{ if eq .Labels.kind "problem" }}{{ $bad = 1 }}{{ end }}{{ end -}}
{{- if eq $bad 1 }}[監控] {{ .Vars.title }}：有問題 ⚠️{{ else }}[監控] {{ .Vars.title }}：全部正常 ✅{{ end -}}
{{ end }}

""" + DIGEST_MESSAGE + r"""
{{ define "line.text.immediate" -}}
{{- if eq .Status "firing" }}🔴 告警{{ else }}✅ 已恢復{{ end }}
{{- range .Alerts }}
{{ if eq .Status "firing" }}{{ .Labels.alertname }}
{{ .Annotations.summary }}{{ else }}{{ .Annotations.resolved_title }}
{{ .Annotations.resolved }}{{ end }}
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
    # one group: every rule is first evaluated on the same tick after a start, so the 12-hour report is there before the
    # startup digest goes out (group_wait 1m; that route never sends again)
    "groups": [{"orgId": 1, "name": "monitoring", "folder": "Alerts", "interval": T["every"], "rules": rules}],
    # file provisioning never deletes a rule that left the file; a uid that does not exist is ignored
    "deleteRules": [{"orgId": 1, "uid": uid} for uid in REMOVED_RULES],
    "contactPoints": [
        {"orgId": 1, "name": "line-push-immediate", "receivers": [line_receiver("line-push-immediate", "line.payload.immediate")]},
        {"orgId": 1, "name": "line-push-startup", "receivers": [
            line_receiver("line-push-startup", "line.payload.digest", title=f"監控啟動 {STARTED}")]},
        {"orgId": 1, "name": "line-push-digest", "receivers": [
            line_receiver("line-push-digest", "line.payload.digest", title="定時彙整")]},
    ],
    "muteTimes": [{"orgId": 1, "name": "digest-times", "time_intervals": [
        {"times": [{"start_time": t, "end_time": hhmm_plus(t, 2)} for t in DIGEST_TIMES], "location": DIGEST_TZ}]},
        {"orgId": 1, "name": "startup-window", "time_intervals": [STARTUP_WINDOW]}],
    "policies": [{"orgId": 1, "receiver": "line-push-immediate", "group_by": ["grafana_folder", "alertname"], "routes": [
        route("line-push-immediate", [["kind", "=", "problem"]], ["..."], T["imm_wait"], T["imm_int"], T["imm_rep"], True),
        route("line-push-startup", [["digest", "=", "all"]], ["digest"], "1m", "1m", "1h", True,
              active_time_intervals=["startup-window"]),
        route("line-push-digest", [["digest", "=", "all"]], ["digest"], "1m", "1m", "1h", False,
              active_time_intervals=["digest-times"])]}],
    "templates": [{"orgId": 1, "name": "digest", "template": TEMPLATE}],
}

print(f"# Alerting (generated for mode={MODE}). Edit timers / texts in the generator, not by hand.")
print(json.dumps(doc, indent=2, ensure_ascii=False))
