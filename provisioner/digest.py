"""The LINE digest, sent by the provisioner (not by Grafana alerting): 3 minutes after the provisioner starts, then at each
DIGEST_TIMES (default 05:00 / 17:00 in DIGEST_TZ). Content: what is wrong now (Grafana's firing alerts, each with its
annotation `digest`), then the last 12 hours from OpenObserve: machine / service downtime and minutes over the usage
thresholds (WARN appears only here).

Scheduling (owner, 2026-10-15): the provisioner calls run() every pass; nothing is queried until next_due has passed. Then
the digest is computed, sent, and next_due moves to the next DIGEST_TIMES after now. next_due lives only in memory: a
restart sends its own startup digest instead of catching up on a missed time. A failed push leaves next_due, so the next
pass tries again; a failed computation is sent as such.
"""
import base64
import datetime
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request
import zoneinfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "grafana"))
from alert_queries import (DIGEST_MAX, MACHINE_REGISTERED, MACHINE_UP, NOW_SECTION, RESOURCES,  # noqa: E402
                           SERVICE_REGISTERED, SERVICE_UP, USAGE_BY, effective, spaced)

TZ = zoneinfo.ZoneInfo(os.environ.get("DIGEST_TZ", "Asia/Taipei"))
TIMES = [datetime.time.fromisoformat(t.strip()) for t in os.environ.get("DIGEST_TIMES", "05:00,17:00").split(",") if t.strip()]
STARTUP_DELAY = datetime.timedelta(minutes=3)  # Grafana starts after the provisioner's first pass and evaluates its rules
REPORT_MIN = 12 * 60  # the report covers the last 12 hours, one point per minute
LINE_LIMIT = 5000  # characters per LINE text message; a longer one is refused as a whole

if os.environ.get("ALERTS_MODE") == "test":
    LINE_URL, LINE_TOKEN, LINE_TO = "http://fake-line:8080/v2/bot/message/push", "TESTTOKEN", "Utest"  # tools/fake_line_server.py
else:
    LINE_URL = os.environ.get("LINE_PUSH_URL") or "https://api.line.me/v2/bot/message/push"
    LINE_TOKEN, LINE_TO = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""), os.environ.get("LINE_USER_ID", "")


def log(msg):
    print(msg, flush=True)


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def next_time(now):
    """the first DIGEST_TIMES after now"""
    today = now.date()
    return min(t for day in (today, today + datetime.timedelta(days=1))
               for t in (datetime.datetime.combine(day, hm, TZ) for hm in TIMES) if t > now)


# ----------------------------------------------------------------------------------------- what is wrong now (Grafana)
def firing_lines():
    """digest lines of the alerts firing in Grafana now, machine -> service -> usage CRIT -> thresholds"""
    auth = base64.b64encode(f"{os.environ['GRAFANA_ADMIN_USER']}:{os.environ['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
    url = (os.environ.get("GRAFANA_URL", "http://grafana:3000/grafana")
           + "/api/alertmanager/grafana/api/v2/alerts?active=true&silenced=false&inhibited=false")
    alerts = [a for a in http("GET", url, {"Authorization": "Basic " + auth}) if a["labels"].get("kind") == "problem"]
    order = {title: i for i, title in enumerate(NOW_SECTION)}
    alerts.sort(key=lambda a: (order.get(a["labels"].get("alertname"), len(order)), sorted(a["labels"].items())))
    return [a["annotations"].get("digest") or a["labels"].get("alertname", "?") for a in alerts]


# ----------------------------------------------------------------------------------------- the last 12 hours (OpenObserve)
def by_key(result, keys):
    """query result -> {(label values of keys): {timestamp: value}}"""
    return {tuple(r["metric"].get(k, "") for k in keys): {int(float(t)): float(v) for t, v in r["values"]}
            for r in result}


def downtime(o2, registered, up, keys, name, end):
    """machines / services still registered: minutes in the window registered without being up"""
    reg, ups = by_key(o2.query_range(registered, end, REPORT_MIN), keys), by_key(o2.query_range(up, end, REPORT_MIN), keys)
    rows = []
    for r in o2.promql(registered):  # still registered now (a retired one has no series)
        key = tuple(r["metric"].get(k, "") for k in keys)
        down = len(reg.get(key, {}).keys() - ups.get(key, {}).keys())
        if down:
            rows.append((key, f"{name(r['metric'])}：停機 {down} 分鐘，可用率 {100 - down * 100 / REPORT_MIN:.1f}%"))
    return [text for _, text in sorted(rows)]


def usage_over(o2, end):
    """per machine and resource: minutes at or over its warn / crit threshold of that minute, the highest value"""
    keys = ("instance", "resource")
    usage = by_key(o2.query_range(USAGE_BY, end, REPORT_MIN), keys)
    warn = by_key(o2.query_range(effective("warn"), end, REPORT_MIN), keys)
    crit = by_key(o2.query_range(effective("crit"), end, REPORT_MIN), keys)
    rows = []
    for (instance, resource), values in usage.items():
        w, c = warn.get((instance, resource), {}), crit.get((instance, resource), {})
        over_warn = sum(1 for t, v in values.items() if t in w and v >= w[t])
        if not over_warn:
            continue
        over_crit = sum(1 for t, v in values.items() if t in c and v >= c[t])
        label, unit = RESOURCES.get(resource, (resource, ""))
        text = f"{instance} 的{spaced(label, False)}：超過警告門檻({w[max(w)]:g}{unit}) {over_warn} 分鐘"
        if over_crit:
            text += f"，其中嚴重({c[max(c)]:g}{unit}) {over_crit} 分鐘"
        order = list(RESOURCES).index(resource) if resource in RESOURCES else len(RESOURCES)
        rows.append((instance, order, resource, text + f"，最高 {max(values.values()):.1f}{unit}"))
    return [row[-1] for row in sorted(rows)]


def report(o2, now):
    end = int(now.timestamp()) // 60 * 60
    return [
        ("機器", downtime(o2, MACHINE_REGISTERED, MACHINE_UP, ("instance",),
                         lambda m: f"{m.get('instance')}（專案 {m.get('service_namespace', '')}）", end)),
        ("服務", downtime(o2, SERVICE_REGISTERED, SERVICE_UP, ("instance", "service"),
                         lambda m: f"{m.get('service')}（在 {m.get('instance')}，專案 {m.get('service_namespace', '')}）", end)),
        ("用量超標", usage_over(o2, end)),
    ]


# ----------------------------------------------------------------------------------------- message
def listed(lines):
    out = "".join(f"\n - {line}" for line in lines[:DIGEST_MAX])
    if len(lines) > DIGEST_MAX:
        out += f"\n （共 {len(lines)} 項，只列前 {DIGEST_MAX} 項）"
    return out


def compose(o2, now, title):
    try:
        now_lines, sections = firing_lines(), report(o2, now)
    except Exception as exc:  # the cause is in the provisioner's log
        log(f"digest   computing failed: {exc}")
        return f"[監控] {title}：彙整產生失敗 ⚠️\n12 小時彙整產生失敗，原因請看 provisioner 的 log。"
    text = f"[監控] {title}：" + ("有問題 ⚠️" if now_lines else "全部正常 ✅") + "\n"
    text += ("目前有問題：" + listed(now_lines)) if now_lines else "目前所有機器與服務都正常。"
    filled = [(name, lines) for name, lines in sections if lines]
    if filled:
        text += "\n\n📋 過去 12 小時彙整" + "\n".join(f"\n【{name}】" + listed(lines) for name, lines in filled)
    else:
        text += "\n\n📋 過去 12 小時：沒有停機，用量也沒有超標。"
    return text[:LINE_LIMIT]


def push(text):
    http("POST", LINE_URL, {"Authorization": "Bearer " + LINE_TOKEN},
         {"to": LINE_TO, "messages": [{"type": "text", "text": text}]})


class Digest:
    def __init__(self, o2):
        self.o2 = o2
        started = datetime.datetime.now(TZ)
        self.next_due = started + STARTUP_DELAY
        self.title = f"監控啟動 {started:%Y-%m-%d %H:%M}"

    def run(self):
        now = datetime.datetime.now(TZ)
        if now < self.next_due:
            return
        push(compose(self.o2, now, self.title))  # raises when LINE fails: next_due stays, the next pass tries again
        self.next_due, self.title = next_time(now), "定時彙整"
        log(f"digest   sent; next {self.next_due:%Y-%m-%d %H:%M}")
