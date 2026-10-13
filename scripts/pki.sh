#!/usr/bin/env bash
# Private CA for agent -> gateway mTLS (no domain needed: agents trust this CA, not the public ones).
#
#   scripts/pki.sh init                       CA, once (pki/ca.key, pki/ca.crt, 10 years). Keep ca.key private.
#   scripts/pki.sh gateway <ip|name> ...      the gateway's server certificate; list every IP / DNS name agents dial
#                                             (local: host.docker.internal; remote: the observer's public IP)
#   scripts/pki.sh client <machine>           one client certificate per machine (CN = machine name, 2 years)
#                                             -> pki/clients/<machine>/{ca.crt,client.crt,client.key}: copy that
#                                             directory to the machine's agent/certs
#
# Everything lands in pki/ (gitignored). The gateway accepts any client certificate this CA signed; the collector has
# no revocation list, so to cut off one machine: re-run `init` (new CA), `gateway`, and `client` for every machine you keep.
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
    sign $PKI/gateway/gateway "/CN=otel-gateway" 825 "$ext"
    cp $PKI/ca.crt $PKI/gateway/ca.crt
    rm -f "$ext"
    echo "gateway: $PKI/gateway/ (SAN ${san%,})"
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
    sed -n '2,13p' "$0"; exit 1
    ;;
esac
