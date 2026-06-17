#!/usr/bin/env bash
# scripts/bp-apply.sh — apply BindPlane Destination + Configuration resources for a demo.
# Usage: scripts/bp-apply.sh --demo <name>
#
# Requires (local):  bindplane CLI installed on the operator's machine.
#                    Apply targets BindPlane Cloud from the operator's machine — NOT the demo VM.
# Requires (.env):   BP_API_KEY, DT_ENV_ID, DT_API_TOKEN
#                    (BP_REMOTE_URL defaults to https://app.bindplane.com)
# Reads:    demos/<demo>/bindplane/destinations.yaml  (applied FIRST)
#           demos/<demo>/bindplane/configurations.yaml (applied SECOND)
# After apply, triggers `bindplane rollout start` for each Configuration (best-effort).
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# ── usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") --demo <name>

Apply BindPlane Destination and Configuration resources for a demo via the bindplane CLI.
Destinations are applied before Configurations (dependency order).

The bindplane CLI must be installed locally (this is an operator-side operation targeting
BindPlane Cloud — it does NOT run on the demo VM).

Options:
  --demo <name>   Demo name (required)
  -h, --help      Show this help message

Examples:
  $(basename "$0") --demo manufacturing
  $(basename "$0") --demo networking
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

# Additional required vars for bp-apply
missing_vars=()
[[ -z "${BP_API_KEY:-}" ]]        && missing_vars+=("BP_API_KEY")
[[ -z "${DT_OTLP_ENDPOINT:-}" ]]  && missing_vars+=("DT_OTLP_ENDPOINT")
[[ -z "${DT_API_TOKEN:-}" ]]      && missing_vars+=("DT_API_TOKEN")
if [[ ${#missing_vars[@]} -gt 0 ]]; then
  err "Required var(s) not set in .env: ${missing_vars[*]}"
  err "BP_API_KEY: BindPlane API key (Settings > API Keys in the BindPlane UI)"
  err "DT_OTLP_ENDPOINT: full Dynatrace OTLP base URL (…/api/v2/otlp) — drives the destination's custom_url"
  err "DT_API_TOKEN: Dynatrace token with metrics.ingest + logs.ingest + openTelemetryTrace.ingest"
  exit 1
fi

demo_exists "$DEMO" || { err "Demo '$DEMO' not found in $REPO/demos/"; exit 1; }

BINDPLANE_DIR="$REPO/demos/$DEMO/bindplane"
DESTINATIONS_YAML="$BINDPLANE_DIR/destinations.yaml"
CONFIGURATIONS_YAML="$BINDPLANE_DIR/configurations.yaml"

for f in "$DESTINATIONS_YAML" "$CONFIGURATIONS_YAML"; do
  [[ -f "$f" ]] || { err "Required blueprint not found: $f"; exit 1; }
done

# ── temp file cleanup (trap) ───────────────────────────────────────────────────
_TMP_FILES=()
_TMP_DIRS=()
_cleanup() {
  for f in "${_TMP_FILES[@]+"${_TMP_FILES[@]}"}"; do
    rm -f "$f"
  done
  for d in "${_TMP_DIRS[@]+"${_TMP_DIRS[@]}"}"; do
    rm -rf "$d"
  done
}
trap _cleanup EXIT

# ── apply_yaml_file <path> <label> ────────────────────────────────────────────
# Substitutes ${DT_ENV_ID} and ${DT_API_TOKEN} into a temp file, then runs
# `bindplane apply -f <temp>`. Fails (exits non-zero) if CLI exits non-zero or
# its output contains the word "invalid" (CLI validation error).
apply_yaml_file() {
  local yaml_file="$1"
  local label="$2"

  info "Applying $label from $(basename "$yaml_file")..."

  # Substitute ${DT_ENV_ID} and ${DT_API_TOKEN} — only these two placeholders.
  # Use a temp DIR (portable across GNU/BSD mktemp) and a .yaml file inside it —
  # a suffixed template like bp-apply-XXXXXX.yaml is not portable to BSD/macOS mktemp.
  local tmp_dir tmp_file
  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/bp-apply.XXXXXX")"
  tmp_file="$tmp_dir/resources.yaml"
  _TMP_FILES+=("$tmp_file")
  _TMP_DIRS+=("$tmp_dir")

  # Use | as sed delimiter — DT_OTLP_ENDPOINT is a URL containing /.
  sed \
    -e "s|\${DT_OTLP_ENDPOINT}|${DT_OTLP_ENDPOINT}|g" \
    -e "s|\${DT_API_TOKEN}|${DT_API_TOKEN}|g" \
    "$yaml_file" > "$tmp_file"

  # Apply via the official CLI. Output is captured to check for "invalid".
  local cli_output
  if ! cli_output="$(bp_cli apply -f "$tmp_file" 2>&1)"; then
    err "bindplane apply failed for $label"
    err "Output: $cli_output"
    return 1
  fi

  # Treat any line containing "invalid" (case-insensitive) as a hard error
  if printf '%s\n' "$cli_output" | grep -qi 'invalid'; then
    err "bindplane apply reported validation error(s) for $label:"
    err "$cli_output"
    return 1
  fi

  info "Results for $label:"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    info "  $line"
  done <<< "$cli_output"
}

# ── start_rollout <config_name> ───────────────────────────────────────────────
# Kicks off a rollout for a Configuration via the CLI. Non-fatal: warns if the
# command errors or reports no agents (rollout is only meaningful once agents connect).
#
# IMPORTANT: --initial/--max default to 1 in BindPlane, which means the rollout
# only ships to ONE agent and then goes Stable, leaving every other matching
# agent permanently unassigned (CONFIGURATION column "-" in `get agent`).
# We pass --initial 100 --max 100 --multiplier 1 so the rollout covers the entire
# demo fleet in a single phase. The Configuration YAML also sets
# spec.rollout.options.phaseAgentCount to the same values, but we set them on
# `start` too as a defense in depth (the CLI flags override server defaults
# even if the spec block was ignored on first apply).
start_rollout() {
  local config_name="$1"
  info "Starting rollout for configuration: $config_name"

  local rollout_output rollout_rc
  rollout_rc=0
  rollout_output="$(bp_cli rollout start "$config_name" \
    --initial 100 --multiplier 1 --max 100 2>&1)" || rollout_rc=$?

  if (( rollout_rc != 0 )); then
    warn "  rollout start '$config_name' exited $rollout_rc: $rollout_output"
    warn "  This is non-fatal — agents will receive config on next OpAMP heartbeat."
  else
    info "  $rollout_output"
  fi
}

# ── main ──────────────────────────────────────────────────────────────────────
info "━━━ BindPlane Apply (via CLI): demo=$DEMO, remote=$BP_REMOTE_URL ━━━"

# Step 1: Apply destinations first (configurations reference them by name)
apply_yaml_file "$DESTINATIONS_YAML" "Destinations"

# Step 2: Apply configurations
apply_yaml_file "$CONFIGURATIONS_YAML" "Configurations"

# Step 2b: Apply fleets (optional — organizes collectors into label-selector views in the UI)
FLEETS_YAML="$BINDPLANE_DIR/fleets.yaml"
if [[ -f "$FLEETS_YAML" ]]; then
  apply_yaml_file "$FLEETS_YAML" "Fleets"
fi

# Step 3: Start rollouts for each Configuration
info "Reading configuration names from $(basename "$CONFIGURATIONS_YAML")..."
config_names="$(yq ea 'select(.kind == "Configuration") | .metadata.name' "$CONFIGURATIONS_YAML" 2>/dev/null || true)"

if [[ -z "$config_names" ]]; then
  warn "No Configuration resources found in $CONFIGURATIONS_YAML — skipping rollout step."
else
  while IFS= read -r cfg_name; do
    [[ -z "$cfg_name" || "$cfg_name" == "null" || "$cfg_name" == "---" ]] && continue
    start_rollout "$cfg_name"
  done <<< "$config_names"
fi

# ── success banner ────────────────────────────────────────────────────────────
cat <<EOF

$(printf '═%.0s' {1..60})
  BindPlane pipelines APPLIED for demo: $DEMO
  Applied via: bindplane CLI → $BP_REMOTE_URL
$(printf '═%.0s' {1..60})

  Resources applied:
    • destinations.yaml  → Destination resources created/updated
    • configurations.yaml → Configuration resources created/updated

  Collectors with matching labels will receive their pipeline
  configuration on the next OpAMP heartbeat (usually <60s).

  Trigger rollout manually:
    bindplane rollout start <configuration-name>

  Optional live demo step:
    See: $REPO/demos/$DEMO/bindplane/rollout.md
    (Add a processor in the UI and watch BindPlane roll it out live)

$(printf '═%.0s' {1..60})
EOF
