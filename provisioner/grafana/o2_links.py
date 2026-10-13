"""Dashboard links from Grafana to OpenObserve's Traces / Logs pages for the dashboard's current time range.

Zoom into a spike (drag on a graph), then click the link: OpenObserve opens that project's stream (= its service.namespace) for exactly that
window. OpenObserve takes from/to in microseconds, Grafana's ${__from}/${__to} are milliseconds, hence the "000".
One pair of links per traces stream that exists in OpenObserve: provisioner/provision.py passes them in O2_STREAMS.
"""
import os

# Relative, so it works on whichever host the browser reached Caddy through (SSH tunnel, Tailscale, ...). Not "/openobserve":
# Grafana prefixes links starting with "/" with its sub path (-> /grafana/openobserve). "../" resolves against the page's
# <base href="/grafana/"> to /openobserve on any dashboard URL (verified on Grafana 13 in a browser, 2026-10-13).
O2_URL = "../openobserve"
O2_ORG = os.environ.get("O2_ORG", "default")
STREAMS = [s for s in os.environ.get("O2_STREAMS", "").split(",") if s]


def links():
    out = []
    for stream in STREAMS:
        for page, label in (("traces", "Traces"), ("logs", "Logs")):
            extra = "&stream_type=logs" if page == "logs" else ""
            out.append({
                "type": "link", "title": f"OpenObserve {label} · {stream}", "icon": "external link",
                "targetBlank": True, "tooltip": f"{label} of {stream} in this dashboard's time range",
                "url": f"{O2_URL}/web/{page}?org_identifier={O2_ORG}&stream={stream}{extra}"
                       "&from=${__from}000&to=${__to}000",
            })
    return out
