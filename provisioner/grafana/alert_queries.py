"""PromQL and names shared by the alert rules (gen_alerts.py) and the digest (provisioner/digest.py), so both read the
same series the same way.

OpenObserve (v1.0.4) quirks, see AGENTS.md "OpenObserve gotchas":
 - `or` / `unless` only match series whose whole label sets are equal (on(...) is not enough once one side went through
   label_replace), so both sides are reduced to {instance, resource} first;
 - a bare selector / aggregation that returns nothing makes `x or <it>` / `x unless <it>` empty, so such operands get
   `+ 0` (no machine has its own thresholds = the normal case; before this, usage alerts then never fired).
"""

# idle time / all CPU time over the same samples: see gen_fleet_overview.py CPU (OpenObserve's rate() over a partly empty
# window made every agent start / stop look like 80-90 % CPU and fired Usage over CRIT)
CPU_BY = "instance,job,service_namespace"
CPU = (f'round(100 * (1 - sum by({CPU_BY})(rate(node_cpu_seconds_total{{mode="idle"}}[5m]))'
       f' / sum by({CPU_BY})(rate(node_cpu_seconds_total[5m]))) * 10) / 10')
# Linux MemAvailable, else macOS (active + wired + compressed) / total. OpenObserve stores metric names lowercased
# (node_memory_MemAvailable_bytes -> node_memory_memavailable_bytes); the mixed-case name finds nothing.
MEM = ('round((100 * (1 - node_memory_memavailable_bytes / node_memory_memtotal_bytes)'
       ' or 100 * (node_memory_active_bytes + node_memory_wired_bytes + node_memory_compressed_bytes) / node_memory_total_bytes) * 10) / 10')
DSEL = 'mountpoint="/",fstype!~"tmpfs|rootfs|overlay"'
DISK = (f'round((100 - 100 * max by(instance,job,service_namespace)(node_filesystem_avail_bytes{{{DSEL}}})'
        f' / max by(instance,job,service_namespace)(node_filesystem_size_bytes{{{DSEL}}})) * 10) / 10')
USAGE = "(" + " or ".join(f'label_replace({e}, "resource", "{r}", "", "")'
                          for e, r in ((CPU, "cpu"), (MEM, "memory"), (DISK, "disk"))) + ")"
USAGE_BY = f'max by(instance,resource)({USAGE})'

# How resources are named and measured in the texts. A new usage kind (e.g. network traffic in GB/m) is one more entry.
RESOURCES = {"cpu": ("CPU", "%"), "memory": ("記憶體", "%"), "disk": ("磁碟", "%")}


def spaced(name, after):
    """a space between a Latin name and the Chinese around it: 「的 CPU 用量」, 「的磁碟用量」"""
    latin = name[0].isascii()
    return (" " if latin else "") + name + (" " if latin and after else "")


def effective(level):
    """threshold per (instance, resource): the machine's own row, else the default row expanded to every machine"""
    own = f'(max by(instance,resource)(usage_threshold_{level}{{instance!="*"}}) + 0)'  # instance = host.name (exporter)
    default = f'usage_threshold_{level}{{instance="*"}}'
    expanded = f'(max by(instance,resource)({USAGE} * 0 + on(resource) group_left() {default}) + 0)'
    return f'({own} or {expanded})'


# Agents push, so a dead machine (or agent) is silence, not up = 0: registered (table machine, exported as
# machine_registered) without an up == 1 sample in the last minute. machine_registered also over the last minute: a retired
# machine stops being exported, and without [1m] its last sample would count for 5 more minutes (no staleness markers).
MACHINE_REGISTERED = 'max by(instance,service_namespace)(max_over_time(machine_registered[1m]))'
MACHINE_UP = 'max by(instance,service_namespace)(max_over_time(up{job="node"}[1m]) == 1)'
# Services the same way: registered (table service) without a successful probe as the newest result in the last 2 minutes.
# [2m]: the agent probes once a minute, so a [1m] window would sometimes hold no sample at all. last_over_time: the newest
# result counts, a failure is not hidden by an older success.
SVC_KEY = "instance,service,service_namespace"
SERVICE_REGISTERED = f'max by({SVC_KEY})(max_over_time(service_registered[1m]))'
SERVICE_UP = f'max by({SVC_KEY})(last_over_time(probe_success{{job="blackbox"}}[2m]) == 1)'

# Rule titles (= alertname). The digest lists the firing ones in this order.
T_MACHINE, T_SERVICE = "機器沒有回應 Machine DOWN", "服務沒有回應 Service DOWN"
T_CRIT, T_THRESHOLDS = "用量超過嚴重門檻 Usage over CRIT", "Grafana 讀不到用量警告門檻資料 Thresholds unavailable"
NOW_SECTION = [T_MACHINE, T_SERVICE, T_CRIT, T_THRESHOLDS]

# The digest's sections list at most this many lines each (LINE: 5000 characters per message, a longer one is refused).
DIGEST_MAX = 20
