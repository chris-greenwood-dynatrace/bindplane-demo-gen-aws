#!/usr/bin/env bash
# scripts/bp-delete.sh — delete BindPlane resources for a demo (best-effort cleanup).
# Usage: scripts/bp-delete.sh --demo <name>
#
# NOTE: BindPlane configurations are cheap to leave in place — deletion is entirely
# optional and intended only for keeping a pristine project between demo switches.
# Running scripts/up.sh --demo <name> is idempotent; configurations will be updated
# on the next apply even if they already exist from a prior run.
#
# What is deleted:
#   • Each Configuration named in demos/<demo>/bindplane/configurations.yaml
#   • Each Destination named in demos/<demo>/bindplane/destinations.yaml
# Sources are inline in configurations; nothing separate to delete.
# Individual not-found errors are warned but do not fail the script.
#
# Requires (local): bindplane CLI installed on the operator's machine.
# Requires (.env):  BP_API_KEY (BP_REMOTE_URL defaults to https://app.bindplane.com)
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# ── usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") --demo <name>

Best-effort cleanup of BindPlane resources for a demo via the bindplane CLI.
Warns on not-found errors but does not fail.

NOTE: This step is OPTIONAL. BindPlane configs are cheap to leave in place.
      Use --purge-bindplane on down.sh to invoke this automatically.

Options:
  --demo <name>   Demo name (required)
  -h, --help      Show this help message

Examples:
  $(basename "$0") --demo manufacturing
EOF
}

# ── parse args ────────────────────────────────────────────────────────────────
DEMO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --demo)
      [[ -n "${2:-}" ]] || { err "--demo requires a value"; exit 1; }
      DEMO="$2"
      shift 2
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

[[ -n "$DEMO" ]] || { err "--demo <name> is required"; usage; exit 1; }

# ── load env ──────────────────────────────────────────────────────────────────
load_env

# ── pre-flight checks ─────────────────────────────────────────────────────────
require_bindplane_cli
require_yq

[[ -z "${BP_API_KEY:-}" ]] && { err "BP_API_KEY not set in .env — cannot delete BindPlane resources"; exit 1; }

demo_exists "$DEMO" || { err "Demo '$DEMO' not found in $REPO/demos/"; exit 1; }

BINDPLANE_DIR="$REPO/demos/$DEMO/bindplane"
CONFIGURATIONS_YAML="$BINDPLANE_DIR/configurations.yaml"
DESTINATIONS_YAML="$BINDPLANE_DIR/destinations.yaml"
FLEETS_YAML="$BINDPLANE_DIR/fleets.yaml"

# ── delete_resource <kind> <name> ─────────────────────────────────────────────
# Runs `bindplane delete <kind_lower> <name>`. Warns on not-found, fails on
# unexpected errors.
delete_resource() {
  local kind="$1"
  local name="$2"
  local kind_lower
  kind_lower="$(printf '%s' "$kind" | tr '[:upper:]' '[:lower:]')"

  info "Deleting $kind '$name'..."
  local del_output del_rc
  del_rc=0
  del_output="$(bp_cli delete "$kind_lower" "$name" 2>&1)" || del_rc=$?

  if (( del_rc != 0 )); then
    # Treat "not found" / "404" as a warning (already gone)
    if printf '%s\n' "$del_output" | grep -qiE 'not found|404|does not exist'; then
      warn "  $kind '$name' not found — already deleted or never applied. Skipping."
    else
      warn "  Failed to delete $kind '$name' (exit $del_rc): $del_output"
      warn "  Continuing cleanup despite this error."
    fi
  else
    info "  $kind '$name' deleted. $del_output"
  fi
}

# ── main ──────────────────────────────────────────────────────────────────────
info "━━━ BindPlane Delete (best-effort cleanup, via CLI): demo=$DEMO ━━━"
warn "This removes BindPlane Configurations and Destinations for demo '$DEMO'."
warn "Re-running 'scripts/up.sh --demo $DEMO' will re-apply them automatically."

# Delete Fleets first (organizational views; no dependents)
if [[ -f "$FLEETS_YAML" ]]; then
  info "Reading Fleet names from fleets.yaml..."
  fleet_names="$(yq ea 'select(.kind == "Fleet") | .metadata.name' "$FLEETS_YAML" 2>/dev/null || true)"
  if [[ -n "$fleet_names" ]]; then
    while IFS= read -r name; do
      [[ -z "$name" || "$name" == "null" || "$name" == "---" ]] && continue
      delete_resource "Fleet" "$name"
    done <<< "$fleet_names"
  fi
fi

# Delete Configurations (they reference Destinations; order matters for clean state)
if [[ -f "$CONFIGURATIONS_YAML" ]]; then
  info "Reading Configuration names from configurations.yaml..."
  config_names="$(yq ea 'select(.kind == "Configuration") | .metadata.name' "$CONFIGURATIONS_YAML" 2>/dev/null || true)"
  if [[ -n "$config_names" ]]; then
    while IFS= read -r name; do
      [[ -z "$name" || "$name" == "null" || "$name" == "---" ]] && continue
      delete_resource "Configuration" "$name"
    done <<< "$config_names"
  else
    warn "No Configuration resources found in $CONFIGURATIONS_YAML"
  fi
else
  warn "$CONFIGURATIONS_YAML not found — skipping Configuration deletion"
fi

# Delete Destinations
if [[ -f "$DESTINATIONS_YAML" ]]; then
  info "Reading Destination names from destinations.yaml..."
  dest_names="$(yq ea 'select(.kind == "Destination") | .metadata.name' "$DESTINATIONS_YAML" 2>/dev/null || true)"
  if [[ -n "$dest_names" ]]; then
    while IFS= read -r name; do
      [[ -z "$name" || "$name" == "null" || "$name" == "---" ]] && continue
      delete_resource "Destination" "$name"
    done <<< "$dest_names"
  else
    warn "No Destination resources found in $DESTINATIONS_YAML"
  fi
else
  warn "$DESTINATIONS_YAML not found — skipping Destination deletion"
fi

info "BindPlane cleanup complete for demo '$DEMO'."
