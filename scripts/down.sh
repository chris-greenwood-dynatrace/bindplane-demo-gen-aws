#!/usr/bin/env bash
# scripts/down.sh — tear down the running demo.
# Usage: scripts/down.sh [--demo <name>] [--purge-bindplane]
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# ── usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Tear down the running demo environment on Azure.

Options:
  --demo <name>        Demo name to destroy (inferred from Terraform state if omitted)
  --purge-bindplane    Also delete BindPlane Configurations and Destinations for this demo.
                       Default: OFF — configs persist server-side and are re-applied on next up.sh.
                       Use this only if you want a fully clean BindPlane project.
  -h, --help           Show this help message

Examples:
  $(basename "$0")
  $(basename "$0") --demo manufacturing
  $(basename "$0") --demo manufacturing --purge-bindplane
EOF
}

# ── parse args ────────────────────────────────────────────────────────────────
DEMO=""
PURGE_BINDPLANE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --demo)
      [[ -n "${2:-}" ]] || { err "--demo requires a value"; exit 1; }
      DEMO="$2"
      shift 2
      ;;
    --purge-bindplane)
      PURGE_BINDPLANE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

# ── load env ──────────────────────────────────────────────────────────────────
load_env

# ── determine demo name ───────────────────────────────────────────────────────
if [[ -z "$DEMO" ]]; then
  # Try reading from terraform state outputs
  DEMO="$(tf output -raw demo 2>/dev/null || true)"
fi

if [[ -z "$DEMO" ]]; then
  # Fall back: inspect tf show JSON for a demo output value
  DEMO="$(tf show -json 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('values',{}).get('outputs',{}).get('demo',{}).get('value',''))" \
    2>/dev/null || true)"
fi

if [[ -z "$DEMO" ]]; then
  err "Could not determine which demo is deployed."
  err "Pass --demo <name> explicitly, e.g.:  scripts/down.sh --demo manufacturing"
  err "Available demos:"
  bash "$REPO/scripts/demos.sh" list 2>/dev/null || true
  exit 1
fi

info "Tearing down demo: $DEMO"

# ── export TF_VARs ────────────────────────────────────────────────────────────
export TF_VAR_demo="${DEMO}"
export TF_VAR_bp_opamp_endpoint="$BP_OPAMP_ENDPOINT"
export TF_VAR_bp_secret_key="$BP_SECRET_KEY"
export TF_VAR_location="${AZURE_LOCATION:-eastus}"
export TF_VAR_vm_size="${VM_SIZE:-Standard_B2s}"

# ssh_public_key needed for destroy plan too
SSH_KEY_PATH="${SSH_PUBLIC_KEY_PATH:-$HOME/.ssh/id_rsa.pub}"
SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"
if [[ -f "$SSH_KEY_PATH" ]]; then
  export TF_VAR_ssh_public_key="$(cat "$SSH_KEY_PATH")"
else
  export TF_VAR_ssh_public_key="placeholder"  # destroy doesn't need the real key
fi

export TF_VAR_admin_source_cidr="${ADMIN_SOURCE_CIDR:-0.0.0.0/0}"

# ── best-effort collector drain ───────────────────────────────────────────────
info "Draining collectors (best-effort, freeing BindPlane cap)..."
PUBLIC_IP="$(tf output -raw public_ip 2>/dev/null || true)"
ADMIN_USER="$(tf output -raw admin_username 2>/dev/null || echo "azureuser")"

if [[ -n "$PUBLIC_IP" ]]; then
  SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes"
  if ssh $SSH_OPTS "$ADMIN_USER@$PUBLIC_IP" \
       "cd /opt/demo/$DEMO && sudo docker compose down" 2>/dev/null; then
    info "Collectors drained successfully."
  else
    warn "Could not SSH to drain collectors — proceeding with destroy anyway."
    warn "BindPlane may show stale agents briefly; they will disappear once enrollment TTL expires."
  fi
else
  warn "Could not determine VM IP — skipping collector drain."
fi

# ── optional: purge BindPlane resources ──────────────────────────────────────
# Run BEFORE terraform destroy so BP_API_KEY is still in env and demo dir exists.
if [[ "$PURGE_BINDPLANE" == "true" ]]; then
  info "Purging BindPlane resources for demo '$DEMO' (--purge-bindplane set)..."
  bash "$REPO/scripts/bp-delete.sh" --demo "$DEMO" || {
    warn "bp-delete.sh encountered errors — continuing with terraform destroy."
    warn "You may need to manually clean up BindPlane resources in the UI."
  }
else
  info "Skipping BindPlane resource cleanup (use --purge-bindplane to remove them)."
fi

# ── terraform destroy ─────────────────────────────────────────────────────────
info "Destroying infrastructure for demo '$DEMO'..."
tf destroy -auto-approve -var "demo=$DEMO"

# ── confirm and remind ────────────────────────────────────────────────────────
info "Resource group destroyed. Azure resources are gone."
info ""
if [[ "$PURGE_BINDPLANE" == "true" ]]; then
  info "BindPlane Configurations and Destinations for demo '$DEMO' were also deleted."
else
  info "NOTE: BindPlane Configurations for demo '$DEMO' persist server-side (intended)."
  info "      They will be re-applied automatically on next 'scripts/up.sh --demo $DEMO'."
  info "      To remove them now: scripts/bp-delete.sh --demo $DEMO"
  info "      Or: scripts/down.sh --demo $DEMO --purge-bindplane"
fi
