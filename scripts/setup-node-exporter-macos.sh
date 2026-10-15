#!/usr/bin/env bash
# Install node_exporter on a macOS agent as a Homebrew service, using the flags in node-exporter/node_exporter.args.
# Re-run after changing that file. node_exporter runs natively (not in Docker): in a container it would
# report the Linux VM of Docker/Colima instead of the Mac.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v brew >/dev/null || { echo "Homebrew is required" >&2; exit 1; }
brew list node_exporter >/dev/null 2>&1 || brew install node_exporter

# The machine's name is not node_exporter's business: the agent labels its metrics with HOST_NAME (agent/.env).
cp node-exporter/node_exporter.args "$(brew --prefix)/etc/node_exporter.args"
brew services restart node_exporter

for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:9100/metrics >/dev/null; then
    echo "node_exporter is up on 127.0.0.1:9100"
    exit 0
  fi
  sleep 1
done
echo "node_exporter did not come up; see $(brew --prefix)/var/log/node_exporter.err.log" >&2
exit 1
