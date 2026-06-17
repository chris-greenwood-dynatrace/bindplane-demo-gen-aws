#!/usr/bin/env bash
# scripts/up.sh — spin up a demo on AWS.
# Usage: scripts/up.sh [--demo <name>] [--skip-validate]
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# ── usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Spin up a demo environment on AWS.

Options:
  --demo <name>      Demo name to deploy (skips interactive picker)
  --skip-validate    Skip static validation before deploy (not recommended)
  -h, --help         Show this help message

Examples:
  $(basename "$0")
  $(basename "$0") --demo manufacturing
  $(basename "$0") --demo manufacturing --skip-validate
EOF
}

# ── parse args ────────────────────────────────────────────────────────────────
DEMO=""
SKIP_VALIDATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --demo)
      [[ -n "${2:-}" ]] || { err "--demo requires a value"; exit 1; }
      DEMO="$2"
      shift 2
      ;;
    --skip-validate)
      SKIP_VALIDATE=true
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

# ── load env + pick demo ──────────────────────────────────────────────────────
load_env  # requires BP_OPAMP_ENDPOINT + BP_SECRET_KEY (+ BP_API_KEY for pipeline apply)

if [[ -z "$DEMO" ]]; then
  DEMO="$(bash "$REPO/scripts/select.sh")"
fi

# ── validate demo exists ──────────────────────────────────────────────────────
demo_exists "$DEMO" || { err "Demo '$DEMO' not found in $REPO/demos/"; exit 1; }

# ── run validate.sh unless --skip-validate ────────────────────────────────────
if [[ "${SKIP_VALIDATE:-false}" != "true" ]]; then
  info "Running static validation for demo '$DEMO'..."
  bash "$REPO/scripts/validate.sh" "$DEMO" || {
    err "Validation failed. Fix issues above or use --skip-validate to bypass (not recommended)."
    exit 1
  }
fi

# ── export TF_VARs ────────────────────────────────────────────────────────────
export TF_VAR_demo="$DEMO"
export TF_VAR_bp_opamp_endpoint="$BP_OPAMP_ENDPOINT"
export TF_VAR_bp_secret_key="$BP_SECRET_KEY"

# Owner tag — auto-derived from $OWNER_TAG (or `whoami`) so multiple operators do
# not collide on AWS instance / VPC / security-group names. See terraform/variables.tf.
OWNER="$(resolve_owner_tag)"
export TF_VAR_owner="$OWNER"
info "Using owner tag: $OWNER (override with OWNER_TAG in .env)"

# SSH public key: read from file path in .env
SSH_KEY_PATH="${SSH_PUBLIC_KEY_PATH:-$HOME/.ssh/id_rsa.pub}"
SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"  # expand tilde
[[ -f "$SSH_KEY_PATH" ]] || { err "SSH public key not found at $SSH_KEY_PATH. Set SSH_PUBLIC_KEY_PATH in .env"; exit 1; }
export TF_VAR_ssh_public_key="$(cat "$SSH_KEY_PATH")"

# Admin CIDR: auto-detect if blank
if [[ -z "${ADMIN_SOURCE_CIDR:-}" ]]; then
  info "ADMIN_SOURCE_CIDR not set — auto-detecting public IP..."
  MY_IP="$(curl -s --max-time 10 https://api.ipify.org)" || { err "Could not detect public IP. Set ADMIN_SOURCE_CIDR in .env"; exit 1; }
  export TF_VAR_admin_source_cidr="${MY_IP}/32"
  info "Using admin CIDR: ${MY_IP}/32"
else
  export TF_VAR_admin_source_cidr="$ADMIN_SOURCE_CIDR"
fi

export TF_VAR_region="${AWS_REGION:-us-east-1}"
export TF_VAR_instance_type="${INSTANCE_TYPE:-t3.medium}"

# ── terraform init + apply ────────────────────────────────────────────────────
info "Initializing Terraform..."
tf init -upgrade

info "Applying Terraform for demo '$DEMO'..."
tf apply -auto-approve -var "demo=$DEMO"

# ── read outputs ──────────────────────────────────────────────────────────────
PUBLIC_IP="$(tf output -raw public_ip)"
ADMIN_USER="$(tf output -raw admin_username)"
info "VM is up at $PUBLIC_IP (user: $ADMIN_USER)"

# ── wait for SSH (timeout 5 min, poll every 10s) ──────────────────────────────
info "Waiting for SSH to become available..."
DEADLINE=$(( $(date +%s) + 300 ))
until ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
         -o ConnectTimeout=5 -o BatchMode=yes \
         "$ADMIN_USER@$PUBLIC_IP" exit 2>/dev/null; do
  [[ $(date +%s) -lt $DEADLINE ]] || { err "Timed out waiting for SSH after 5 minutes."; exit 1; }
  info "  SSH not ready yet, retrying in 10s..."
  sleep 10
done
info "SSH is up."

# ── wait for cloud-init done marker ──────────────────────────────────────────
info "Waiting for cloud-init to complete (/opt/demo/CLOUD_INIT_DONE)..."
DEADLINE=$(( $(date +%s) + 300 ))
until ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
         -o ConnectTimeout=5 -o BatchMode=yes \
         "$ADMIN_USER@$PUBLIC_IP" "test -f /opt/demo/CLOUD_INIT_DONE" 2>/dev/null; do
  [[ $(date +%s) -lt $DEADLINE ]] || { err "Timed out waiting for cloud-init (5 min). Check VM logs."; exit 1; }
  info "  cloud-init still running, retrying in 15s..."
  sleep 15
done
info "Cloud-init complete."

# ── sync demo files to VM ─────────────────────────────────────────────────────
info "Syncing demo files to VM..."
# rsync preferred; fall back to scp -r
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
if command -v rsync &>/dev/null; then
  rsync -az -e "ssh $SSH_OPTS" \
    "$REPO/demos/$DEMO/" \
    "$ADMIN_USER@$PUBLIC_IP:/opt/demo/$DEMO/"
else
  warn "rsync not found, falling back to scp"
  ssh $SSH_OPTS "$ADMIN_USER@$PUBLIC_IP" "mkdir -p /opt/demo/$DEMO"
  scp $SSH_OPTS -r "$REPO/demos/$DEMO/." "$ADMIN_USER@$PUBLIC_IP:/opt/demo/$DEMO/"
fi
info "Demo files synced."

# ── write .env to VM ─────────────────────────────────────────────────────────
info "Writing /opt/demo/.env on VM..."
# Build env content: BP_* for collector OpAMP enrollment; DT_* used by BindPlane destinations.
ENV_CONTENT="BP_OPAMP_ENDPOINT=${BP_OPAMP_ENDPOINT}
BP_SECRET_KEY=${BP_SECRET_KEY}
DT_ENV_ID=${DT_ENV_ID:-}
DT_API_TOKEN=${DT_API_TOKEN:-}
"
ssh $SSH_OPTS "$ADMIN_USER@$PUBLIC_IP" "sudo tee /opt/demo/.env > /dev/null" <<< "$ENV_CONTENT"
ssh $SSH_OPTS "$ADMIN_USER@$PUBLIC_IP" "sudo chmod 600 /opt/demo/.env"
info "/opt/demo/.env written."

# ── start docker compose ──────────────────────────────────────────────────────
info "Starting docker compose for demo '$DEMO'..."
ssh $SSH_OPTS "$ADMIN_USER@$PUBLIC_IP" \
  "cd /opt/demo/$DEMO && sudo docker compose --env-file /opt/demo/.env up -d"
info "Collectors and simulators started."

# ── apply BindPlane pipeline configurations via the bindplane CLI ─────────────
# NOTE: `bindplane` must be installed locally on the operator's machine.
#       Apply targets BindPlane Cloud directly from here — NOT from the demo VM.
# Requires BP_API_KEY in .env. Calls bp-apply.sh which:
#   1. Runs `bindplane apply -f destinations.yaml` (creates/updates Destination resources)
#   2. Runs `bindplane apply -f configurations.yaml` (creates/updates Configuration resources)
#   3. Runs `bindplane rollout start <name>` for each Configuration (best-effort)
# This is idempotent: re-running just updates existing resources.
info "Applying BindPlane pipeline configurations via the bindplane CLI..."
bash "$REPO/scripts/bp-apply.sh" --demo "$DEMO" || {
  err "BindPlane apply failed. Collectors are running but pipelines may not be active."
  err "Fix any errors above, then re-run: scripts/bp-apply.sh --demo $DEMO"
  err "Ensure 'bindplane' CLI is installed: brew tap observiq/bindplane && brew install bindplane"
  err "Or build pipelines manually in the UI: $REPO/demos/$DEMO/bindplane/rollout.md"
  # Non-fatal: infrastructure is up; user can fix and rerun bp-apply.sh independently.
}

# ── print next steps ──────────────────────────────────────────────────────────
cat <<EOF

$(tput bold 2>/dev/null || true)═══════════════════════════════════════════════════════$(tput sgr0 2>/dev/null || true)
  Demo '$DEMO' is LIVE on $PUBLIC_IP
$(tput bold 2>/dev/null || true)═══════════════════════════════════════════════════════$(tput sgr0 2>/dev/null || true)

NEXT STEPS:
  1. Open BindPlane UI → https://app.bindplane.com
     Confirm collectors for demo=$DEMO appear as connected (may take ~60s).
     Pipelines were applied automatically — no manual UI build required.

  2. Verify telemetry in Dynatrace (allow ~2 min for first data):
     https://${DT_ENV_ID:-<your-env-id>}.live.dynatrace.com

  3. OPTIONAL — Live Rollout demo (show BindPlane rolling a change fleet-wide):
     See: $REPO/demos/$DEMO/bindplane/rollout.md

  4. SSH to VM:       scripts/ssh.sh
  5. Tail logs:       scripts/logs.sh --demo $DEMO
  6. Tear down:       scripts/down.sh --demo $DEMO

EOF
