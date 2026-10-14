#!/usr/bin/env bash
# Private CA for agent -> gateway mTLS (no domain needed: agents trust this CA, not the public ones).
# Signing is the collector's job: run this on the collector (repo root there); the CA lives in its pki/ (gitignored),
# next to the gateway's certificate. Steps: docs/collector-runbook.md step 3, docs/agent-runbook.md step 1.
#
#   scripts/pki.sh init                       CA, once (pki/ca.key, pki/ca.crt, 10 years). Keep ca.key private, back it up.
#   scripts/pki.sh gateway <ip|name> ...      the gateway's key + server certificate -> pki/gateway/ (825 days); list
#                                             every IP / DNS name agents dial (the collector's public IP, 127.0.0.1 for
#                                             its own agent; local mode: host.docker.internal). Linux collector:
#                                             afterwards sudo chown 10001:10001 pki/gateway/gateway.key
#   scripts/pki.sh sign < client.csr > client.crt
#                                             an agent's certificate from its CSR (2 years; the key stays on the agent)
#   scripts/pki.sh client <machine>           local mode only (collector + agent on one machine): key + certificate
#                                             -> pki/clients/<machine>/{ca.crt,client.crt,client.key} (AGENT_CERTS_DIR)
#
# The gateway accepts any client certificate this CA signed; the collector has no revocation list, so to cut off one
# machine: move pki/ away, re-run `init` (new CA), `gateway`, and sign a new certificate for every machine you keep.
set -euo pipefail
cd "$(dirname "$0")/.."
PKI=pki
mkdir -p "$PKI"

die() { echo "$*" >&2; exit 1; }
need_ca() { [[ -f $PKI/ca.key && -f $PKI/ca.crt ]] || die "no CA yet: run scripts/pki.sh init"; }

sign() { # sign <out-prefix> <subject> <days> <extfile>
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout "$1.key" -out "$1.csr" -subj "$2" 2>/dev/null
  openssl x509 -req -in "$1.csr" -CA $PKI/ca.crt -CAkey $PKI/ca.key -CAcreateserial -days "$3" -sha256 -extfile "$4" -out "$1.crt" 2>/dev/null
  rm -f "$1.csr"
  chmod 600 "$1.key"
}

case "${1:-}" in
  init)
    [[ -f $PKI/ca.key ]] && die "$PKI/ca.key already exists (delete pki/ to start over)"
    openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 -sha256 \
      -subj "/CN=observability agent CA" -keyout $PKI/ca.key -out $PKI/ca.crt 2>/dev/null
    chmod 600 $PKI/ca.key
    echo "CA: $PKI/ca.crt"
    ;;
  gateway)
    need_ca; shift; [[ $# -gt 0 ]] || die "usage: scripts/pki.sh gateway <ip|name> ..."
    san=""
    for n in "$@"; do
      if [[ $n =~ ^[0-9.]+$ || $n == *:* ]]; then san+="IP:$n,"; else san+="DNS:$n,"; fi
    done
    ext=$PKI/.ext.$$
    printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature\n' "${san%,}" > "$ext"
    mkdir -p $PKI/gateway
    rm -f $PKI/gateway/gateway.key # may be owned by the container's user (10001); the directory is ours
    sign $PKI/gateway/gateway "/CN=otel-gateway" 825 "$ext"
    cp $PKI/ca.crt $PKI/gateway/ca.crt
    rm -f "$ext"
    echo "gateway: $PKI/gateway/ (SAN ${san%,})"
    ;;
  sign)
    need_ca
    tmp=$PKI/.sign.$$; mkdir "$tmp"; trap 'rm -rf "$tmp"' EXIT
    cat > "$tmp/client.csr"
    openssl req -in "$tmp/client.csr" -noout -verify 2>/dev/null || die "stdin is not a valid CSR"
    printf 'extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n' > "$tmp/ext"
    openssl x509 -req -in "$tmp/client.csr" -CA $PKI/ca.crt -CAkey $PKI/ca.key -CAcreateserial -days 730 -sha256 \
      -extfile "$tmp/ext" -out "$tmp/client.crt" 2>/dev/null
    openssl x509 -in "$tmp/client.crt" -noout -subject -enddate >&2
    cat "$tmp/client.crt"
    ;;
  client)
    need_ca; [[ -n ${2:-} ]] || die "usage: scripts/pki.sh client <machine>"
    [[ $2 =~ ^[A-Za-z0-9._-]+$ ]] || die "machine name: letters, digits, . _ - only"
    ext=$PKI/.ext.$$
    printf 'extendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\n' > "$ext"
    dir=$PKI/clients/$2
    mkdir -p "$dir"
    sign "$dir/client" "/CN=$2" 730 "$ext"
    cp $PKI/ca.crt "$dir/ca.crt"
    rm -f "$ext"
    echo "client $2: $dir/ (copy to that machine's agent/certs)"
    ;;
  *)
    sed -n '2,19p' "$0"; exit 1
    ;;
esac
