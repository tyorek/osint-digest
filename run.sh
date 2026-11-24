#!/bin/bash
# osint-digest -- nightly cross-outlet US policy digest + trending world stories,
# written by the local model (see CLAUDE.md and osint.py). Cron: 30 4 * * *.
set -uo pipefail
source /home/agent/.config/agent.env
# Uptime Kuma heartbeat (same pattern as the other agents): KUMA_PUSH_OSINT_DIGEST in agent.env.
KUMA_VAR="KUMA_PUSH_$(basename "$(dirname "$(readlink -f "$0")")" | tr 'a-z-' 'A-Z_')"
kuma_push() {
    local rc=$?
    local url="${!KUMA_VAR:-}"
    [ -n "$url" ] || return 0
    local status=up
    [ "$rc" -eq 0 ] || status=down
    curl -fsS -m 10 -o /dev/null "${url}?status=${status}&msg=exit%20${rc}" || true
}
trap kuma_push EXIT
cd "$(dirname "$(readlink -f "$0")")" || exit 1
mkdir -p reports logs state
exec 9>state/run.lock
flock -n 9 || { echo "$(date -Is): [osint] already running"; exit 1; }
echo "$(date -Is): [osint] start"
python3 ./osint.py
rc=$?

# Publish today's report to the Homepage "News digest" tile (same write-only rrsync account the
# daily digest uses; lands as /srv/digest/incoming/news.md on the Homepage CT). A failed push only
# logs a warning -- the tile's stale marker shows it -- and never changes the run's exit status.
REPORT="reports/$(date +%F).md"
if [ "$rc" -eq 0 ] && [ -n "${DIGEST_PUSH_TARGET:-}" ] && [ -s "$REPORT" ]; then
    if rsync -t --chmod=F644 -e "ssh -i /home/agent/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" \
            "$REPORT" "${DIGEST_PUSH_TARGET}:news.md" 2>>logs/publish.log; then
        echo "$(date -Is): [osint] published to ${DIGEST_PUSH_TARGET}:news.md"
    else
        echo "$(date -Is): [osint] WARNING: could not publish news.md, see logs/publish.log" >&2
    fi
fi
exit $rc
