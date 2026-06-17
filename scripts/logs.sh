#!/usr/bin/env bash
# scripts/logs.sh — Tail docker compose logs from the running demo VM.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [--demo <name>] [SERVICE]

Tail docker compose logs on the running demo VM.

Options:
  --demo <name>   Demo to target (required if terraform output does not
                  include a 'demo' output; omit when only one demo is deployed)
  -h, --help      Show this help

Arguments:
  SERVICE         Optional docker compose service name to filter logs
                  (e.g. gateway, line-a, device-line-a). Omit to tail all services.

Examples:
  $(basename "$0")                   # tail all services for the active demo
  $(basename "$0") --demo manufacturing
  $(basename "$0") --demo manufacturing gateway
  $(basename "$0") gateway           # tail a single service
EOF
  exit 0
}

# ── Parse arguments ───────────────────────────────────────────────────────────
DEMO=""
SERVICE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --demo)
      [[ -z "${2:-}" ]] && { err "--demo requires a value"; exit 1; }
      DEMO="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    -*)
      err "Unknown option: $1"
      usage
      ;;
    *)
      if [[ -z "$SERVICE" ]]; then
        SERVICE="$1"
      else
        err "Unexpected positional argument: $1"
        usage
      fi
      shift
      ;;
  esac
done

# ── Load environment ──────────────────────────────────────────────────────────
load_env --skip-secrets

# ── Resolve DEMO ──────────────────────────────────────────────────────────────
if [[ -z "$DEMO" ]]; then
  # Try to read from terraform state; terraform may not expose a 'demo' output.
  DEMO="$(tf output -raw demo 2>/dev/null || true)"

  if [[ -z "$DEMO" ]]; then
    # Fall back to listing available demos and asking the user to specify.
    info "Could not determine the active demo from terraform output."
    info "Available demos in $REPO/demos/:"
    find "$REPO/demos" -mindepth 2 -maxdepth 2 -name "manifest.yaml" \
      | sort \
      | grep -v '/demos/_' \
      | while read -r m; do
          name="$(basename "$(dirname "$m")")"
          printf '  • %s\n' "$name" >&2
        done
    err "Re-run with --demo <name>  (e.g. scripts/logs.sh --demo manufacturing)"
    exit 1
  fi
fi

# Validate the demo exists locally (best-effort; VM may still be running)
if ! demo_exists "$DEMO"; then
  warn "Demo '$DEMO' not found in $REPO/demos/ — continuing anyway."
fi

# ── Read terraform outputs ────────────────────────────────────────────────────
PUBLIC_IP="$(tf output -raw public_ip 2>/dev/null || true)"
ADMIN_USER="$(tf output -raw admin_username 2>/dev/null || true)"
ADMIN_USER="${ADMIN_USER:-azureuser}"

if [[ -z "$PUBLIC_IP" ]]; then
  err "No running VM found. Run scripts/up.sh first."
  exit 1
fi

# ── Tail logs over SSH ────────────────────────────────────────────────────────
info "Tailing logs for demo '$DEMO'${SERVICE:+ (service: $SERVICE)} on $PUBLIC_IP …"
info "Press Ctrl+C to stop."

# SERVICE is intentionally unquoted at the end so an empty value adds nothing.
# shellcheck disable=SC2086
exec ssh \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  "$ADMIN_USER@$PUBLIC_IP" \
  "cd /opt/demo/${DEMO} && sudo docker compose --env-file /opt/demo/.env logs -f ${SERVICE:-}"
