#!/usr/bin/env bash
# Split-tunnel TorGuard connection for the live and canary CI jobs.
#
# Only wiki.isocpp.org crosses the VPN. Everything the runner itself depends on
# (the Actions service, apt, PyPI, artifact upload) stays on the direct path, so
# a slow or dropped tunnel cannot stall or fail the job for unrelated reasons.
#
# Usage:
#   bash scripts/ci/torguard_vpn.sh connect
#   bash scripts/ci/torguard_vpn.sh verify
#   bash scripts/ci/torguard_vpn.sh disconnect
#
# connect requires, from the live-wiki environment:
#   TORGUARD_VPN_USERNAME    VPN credentials, not the torguard.net site login
#   TORGUARD_VPN_PASSWORD
#   TORGUARD_VPN_LOCATION    bundle basename, e.g. TorGuard.USA-MIAMI
#
# Runs on a GitHub-hosted runner because the runner user has passwordless sudo
# and /dev/net/tun is usable directly. Jobs running inside a `container:` are a
# different matter and would need --privileged.

set -euo pipefail

WIKI_HOST="${TORGUARD_VPN_WIKI_HOST:-wiki.isocpp.org}"
WORK_DIR="${RUNNER_TEMP:-/tmp}/torguard-vpn"
CONF="${WORK_DIR}/torguard.conf"
CREDS="${WORK_DIR}/credentials.txt"
CA_FILE="${WORK_DIR}/ca.crt"
PIDFILE="${WORK_DIR}/vpn.pid"
LOGFILE="${WORK_DIR}/vpn.log"
DIRECT_IP_FILE="${WORK_DIR}/direct-egress-ip"
WIKI_IPS_FILE="${WORK_DIR}/wiki-ips"
HOSTS_MARKER="# torguard-vpn-ci"
BUNDLE_URL="${TORGUARD_VPN_BUNDLE_URL:-https://torguard.net/downloads/OpenVPN-UDP-Linux.zip}"
API_PROBE="https://${WIKI_HOST}/api.php?action=query&meta=siteinfo&siprop=general&format=json"

log() { printf 'torguard_vpn: %s\n' "$*"; }
die() { printf 'torguard_vpn: %s\n' "$*" >&2; exit 1; }

# Probe the edge as the tests do. curl's own User-Agent is a different client as
# far as a UA-keyed rule is concerned, which could pass verify and fail pytest,
# or the reverse. Asking the package keeps the version from drifting; the
# fallback only matters when the script runs outside the installed environment,
# where the product token is the part any such rule would key on anyway.
probe_user_agent() {
    local py
    for py in python python3; do
        if command -v "$py" >/dev/null 2>&1; then
            "$py" -c 'from wg21_wiki_mcp.config import default_user_agent; print(default_user_agent())' 2>/dev/null \
                && return 0
        fi
    done
    printf '%s\n' "wg21-wiki-mcp (+https://github.com/cppalliance/wg21-wiki-mcp)"
}

# Deliberately not a Cloudflare-fronted service. The split proof rests on this
# address never landing in the routed set, and wiki.isocpp.org is behind
# Cloudflare, so a shared anycast range would make the check read the tunnel exit
# and report a broken split that is in fact fine.
egress_ip() {
    local ip
    ip="$(curl -fsS --max-time 20 https://checkip.amazonaws.com)" || return 1
    ip="${ip//[$'\r\n ']/}"
    # curl exits 0 on a 200 with an empty body, and an empty string compares equal
    # to itself, so verify would pass having compared nothing.
    printf '%s' "$ip" | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || return 1
    printf '%s' "$ip"
}

dump_log() {
    log "last 40 lines of ${LOGFILE}:"
    sudo tail -40 "$LOGFILE" 2>/dev/null || true
}

remove_hosts_pin() {
    if grep -qF "$HOSTS_MARKER" /etc/hosts 2>/dev/null; then
        sudo sed -i "\|${HOSTS_MARKER}|d" /etc/hosts
    fi
}

# Both ship on ubuntu-latest today and neither is guaranteed to tomorrow. unzip
# is the one that bites quietly: it is used by fetch_config rather than here, so
# a missing one would surface mid-connect as a bare "command not found".
install_dependencies() {
    # Not named `missing`: cmd_connect has a string by that name, and shellcheck
    # reads the two as one variable and reports the array use as an error.
    local -a missing_pkgs=()
    command -v openvpn >/dev/null 2>&1 || missing_pkgs+=(openvpn)
    command -v unzip >/dev/null 2>&1 || missing_pkgs+=(unzip)

    if [ "${#missing_pkgs[@]}" -gt 0 ]; then
        log "installing: ${missing_pkgs[*]}"
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing_pkgs[@]}"
    fi

    local cmd
    for cmd in openvpn unzip; do
        command -v "$cmd" >/dev/null 2>&1 \
            || die "${cmd} is missing and apt-get did not provide it. The runner image no longer ships it; add it to the install list in this script."
    done
    log "openvpn present: $(openvpn --version 2>&1 | head -1 || true)"
}

fetch_config() {
    curl -fsSL --max-time 120 "$BUNDLE_URL" -o "${WORK_DIR}/bundle.zip" \
        || die "could not download the OpenVPN bundle from ${BUNDLE_URL}. Check whether torguard.net is reachable, then re-run the job."
    rm -rf "${WORK_DIR}/bundle"
    unzip -oq "${WORK_DIR}/bundle.zip" -d "${WORK_DIR}/bundle"

    local src
    src="$(find "${WORK_DIR}/bundle" -type f -name "${TORGUARD_VPN_LOCATION}.ovpn" -print -quit)"
    if [ -z "$src" ]; then
        log "available locations:"
        find "${WORK_DIR}/bundle" -type f -name '*.ovpn' -exec basename {} .ovpn \; | sort | sed 's/^/  /'
        die "TORGUARD_VPN_LOCATION='${TORGUARD_VPN_LOCATION}' is not in the bundle. Set it to one of the basenames listed above."
    fi
    cp "$src" "$CONF"

    # A plain `[ -n "$ca" ] && cp ...` here would be the function's last command,
    # so an empty result would return 1 and abort the whole script with no output.
    local ca
    ca="$(find "${WORK_DIR}/bundle" -type f -name 'ca.crt' -print -quit)"
    if [ -n "$ca" ]; then
        cp "$ca" "$CA_FILE"
    fi
}

# TorGuard's bundled configs predate OpenVPN 2.6 and do not start as shipped on
# ubuntu-latest. The CRLF strip has to come first: '^ncp-disable$' never matches
# 'ncp-disable\r', so every later edit silently does nothing without it.
patch_config_for_openvpn26() {
    sed -i 's/\r$//' "$CONF"
    sed -i '/^ncp-disable$/d' "$CONF"     # removed in 2.6, now a fatal option
    sed -i '/^compress$/d' "$CONF"        # restores DCO; 2.6 refuses compression anyway
    grep -qE '^(client|tls-client)$' "$CONF" || sed -i '1i client' "$CONF"
    if [ -f "$CA_FILE" ]; then
        sed -i "s#^ca .*#ca ${CA_FILE}#" "$CONF"
    fi

    # The bundle is refetched from torguard.net on every run with no checksum to
    # pin it, and every config in it ships `script-security 2` with root --up and
    # --down hooks. openvpn here runs under sudo on a runner holding four wiki
    # credentials, so a downloaded file must not get to name programs for it to
    # execute. The hooks only manage resolv.conf, which this split tunnel does not
    # want touched, and --route-nopull already discards a *pushed* redirect-gateway
    # but not one written into the config, so drop that too.
    sed -i '/^script-security /d; /^up /d; /^down /d; /^redirect-gateway/d' "$CONF"
}

write_credentials() {
    (
        umask 077
        printf '%s\n%s\n' "$TORGUARD_VPN_USERNAME" "$TORGUARD_VPN_PASSWORD" > "$CREDS"
    )
    # Replace rather than rewrite in place: a bundle that already carries a path
    # on the directive would otherwise slip past a substitution keyed to the bare
    # form, and openvpn --daemon with no credentials file blocks on a prompt no
    # one can answer until the job times out.
    sed -i '/^auth-user-pass/d' "$CONF"
    printf 'auth-user-pass %s\n' "$CREDS" >> "$CONF"
}

cmd_connect() {
    local missing="add it to the live-wiki GitHub environment; see CONTRIBUTING.md"
    : "${TORGUARD_VPN_USERNAME:?is not set. ${missing}}"
    : "${TORGUARD_VPN_PASSWORD:?is not set. ${missing}}"
    : "${TORGUARD_VPN_LOCATION:?is not set, e.g. TorGuard.USA-MIAMI. ${missing}}"

    mkdir -p "$WORK_DIR"
    install_dependencies

    # Record the direct egress address before anything is routed. verify then
    # asserts it is unchanged, which is what proves the tunnel is split rather
    # than carrying the whole job.
    local direct
    direct="$(egress_ip)" \
        || die "could not read a valid address from checkip.amazonaws.com. Re-run the job; if it persists the runner has no direct egress and the split cannot be proven."

    printf '%s\n' "$direct" > "$DIRECT_IP_FILE"
    log "direct egress IP recorded: ${direct}"

    # Clear any pin before resolving: getent reads /etc/hosts first, so on a
    # reused runner a stale pin would be re-adopted and never refreshed from DNS.
    remove_hosts_pin

    # Resolve once and pin. Cloudflare hands out a small rotating A set on a
    # short TTL and Python re-resolves per connection, so without the pin a
    # mid-run rotation would produce an address with no tunnel route, which
    # would quietly egress direct and come back 403. Pinning only A records also
    # keeps glibc from ever reaching DNS for an AAAA that has no route.
    local -a wiki_ips
    mapfile -t wiki_ips < <(getent ahostsv4 "$WIKI_HOST" | awk '{print $1}' | sort -u)
    [ "${#wiki_ips[@]}" -gt 0 ] \
        || die "could not resolve ${WIKI_HOST}. Check runner DNS; the tunnel cannot be scoped without an address list."
    printf '%s\n' "${wiki_ips[@]}" > "$WIKI_IPS_FILE"
    log "routing ${WIKI_HOST} over the tunnel: ${wiki_ips[*]}"

    for ip in "${wiki_ips[@]}"; do
        printf '%s %s %s\n' "$ip" "$WIKI_HOST" "$HOSTS_MARKER"
    done | sudo tee -a /etc/hosts >/dev/null

    fetch_config
    patch_config_for_openvpn26
    write_credentials

    # --route-nopull drops the server's pushed redirect-gateway so the default
    # route never moves; the explicit per-address routes are then the only
    # traffic the tunnel carries. Using OpenVPN's own --route rather than
    # `ip route add` means they appear only once the tunnel is up and are torn
    # down with the process, with no ordering or cleanup left to get wrong.
    local -a route_args=()
    for ip in "${wiki_ips[@]}"; do
        route_args+=(--route "$ip" 255.255.255.255 vpn_gateway)
    done

    rm -f "$LOGFILE"
    sudo openvpn \
        --config "$CONF" \
        --route-nopull \
        "${route_args[@]}" \
        --verb 3 \
        --daemon \
        --log "$LOGFILE" \
        --writepid "$PIDFILE"

    # openvpn --log opens the file 0600 as root, and actions/upload-artifact runs
    # as the runner user, so without this the log the runbook promises on failure
    # never uploads.
    sudo chmod 0644 "$LOGFILE" 2>/dev/null || true

    local pid=""
    for _ in $(seq 1 60); do
        if sudo grep -q "Initialization Sequence Completed" "$LOGFILE" 2>/dev/null; then
            break
        fi
        # --daemon means the foreground process exits 0 the moment the child
        # forks, so a fatal error afterwards (rejected credentials being the
        # common one) would otherwise burn the full 120s and be reported as a
        # timeout, which points at the wrong remedy.
        pid="$(cat "$PIDFILE" 2>/dev/null || true)"
        if [ -n "$pid" ] && ! sudo kill -0 "$pid" 2>/dev/null; then
            dump_log
            die "the openvpn daemon exited during startup. The log above gives the reason; AUTH_FAILED means the TORGUARD_VPN_USERNAME/TORGUARD_VPN_PASSWORD secrets are wrong."
        fi
        sleep 2
    done

    if ! sudo grep -q "Initialization Sequence Completed" "$LOGFILE" 2>/dev/null; then
        dump_log
        die "tunnel did not come up within 120s. Check the log above, then re-run; if it repeats, try another TORGUARD_VPN_LOCATION."
    fi

    # A route that fails to install does not make the initialization line say so:
    # add_routes reports through ISC_ROUTE_ERRORS, and only ISC_ERRORS adds the
    # "With Errors" suffix. So the tunnel can come up "clean" while carrying
    # nothing, which is indistinguishable from success without this check.
    if sudo grep -qiE "route (add command|addition) failed" "$LOGFILE" 2>/dev/null; then
        dump_log
        die "the tunnel came up but at least one route failed to install, so wiki traffic would egress direct. See the log above."
    fi
    log "tunnel up"
}

cmd_verify() {
    [ -f "$DIRECT_IP_FILE" ] || die "no recorded egress IP. Run the connect subcommand first."
    [ -f "$WIKI_IPS_FILE" ] || die "no recorded wiki addresses. Run the connect subcommand first."

    local expected actual
    expected="$(cat "$DIRECT_IP_FILE")"
    actual="$(egress_ip)" \
        || die "could not read a valid address from checkip.amazonaws.com. Re-run the job; the split cannot be confirmed without it."
    if [ "$actual" != "$expected" ]; then
        die "general egress moved from ${expected} to ${actual}. --route-nopull did not take and the whole job is on the tunnel; fix the split before relying on these results."
    fi
    log "general egress unchanged (${actual}), so the split held"

    local ip route dev
    while read -r ip; do
        [ -n "$ip" ] || continue
        route="$(ip route get "$ip" 2>/dev/null || true)"
        dev="$(printf '%s' "$route" | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
        case "$dev" in
            tun*) log "${ip} routes via ${dev}" ;;
            *) die "${ip} routes via '${dev:-unknown}' rather than a tun device, so wiki traffic would egress direct and be blocked. The tunnel is up but its routes did not install; see the uploaded VPN log and re-run the job." ;;
        esac
    done < "$WIKI_IPS_FILE"

    local agent status
    agent="$(probe_user_agent)"
    log "probing the wiki API as '${agent}'"
    status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -A "$agent" "$API_PROBE" || true)"
    # curl writes 000 when it never got an HTTP response at all, which the route
    # check above cannot rule out: the tunnel can drop between the two. Reporting
    # that as a blocked exit would send an operator rotating locations, which
    # would not fix it.
    if [ -z "$status" ] || [ "$status" = "000" ]; then
        die "no HTTP response from ${WIKI_HOST} through the tunnel within 30s. The routes are installed, so this is a tunnel that dropped or stalled rather than a blocked exit: see the uploaded VPN log and re-run the job."
    fi
    if [ "$status" != "200" ]; then
        die "the wiki API returned ${status} through the tunnel. The tunnel is up and routing correctly, so this TorGuard exit IP is itself WAF-blocked: rotate TORGUARD_VPN_LOCATION to a different location."
    fi
    log "wiki API returned 200 over the tunnel"
}

# Never fails: this runs under `if: always()` and must not turn a passing job
# red, nor mask the real failure of a job that is already going red.
cmd_disconnect() {
    set +e
    if [ -f "$PIDFILE" ]; then
        local pid
        pid="$(cat "$PIDFILE" 2>/dev/null)"
        if [ -n "$pid" ] && sudo kill -0 "$pid" 2>/dev/null; then
            sudo kill "$pid" 2>/dev/null
            for _ in $(seq 1 10); do
                sudo kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            sudo kill -9 "$pid" 2>/dev/null
        fi
        sudo rm -f "$PIDFILE"
    fi
    remove_hosts_pin
    rm -f "$CREDS"
    log "disconnected"
    return 0
}

case "${1:-}" in
    connect) cmd_connect ;;
    verify) cmd_verify ;;
    disconnect) cmd_disconnect ;;
    *) die "usage: $0 {connect|verify|disconnect}" ;;
esac
