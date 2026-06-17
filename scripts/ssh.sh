#!/usr/bin/env bash
# scripts/ssh.sh — SSH into the running demo VM.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

load_env --skip-secrets

# ── Read terraform outputs ────────────────────────────────────────────────────
PUBLIC_IP="$(tf output -raw public_ip 2>/dev/null || true)"
ADMIN_USER="$(tf output -raw admin_username 2>/dev/null || true)"

# Default admin username if terraform has not yet set it
ADMIN_USER="${ADMIN_USER:-ubuntu}"

if [[ -z "$PUBLIC_IP" ]]; then
  err "No running VM found. Run scripts/up.sh first."
  exit 1
fi

info "Connecting to $ADMIN_USER@$PUBLIC_IP …"

exec ssh \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  "$ADMIN_USER@$PUBLIC_IP" \
  "$@"
