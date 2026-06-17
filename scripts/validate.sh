#!/usr/bin/env bash
# scripts/validate.sh — static validation of a demo before AWS spend.
# Usage: scripts/validate.sh <demo-name>
# Exits 0 if all checks PASS (WARNs ok), 1 if any FAIL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

# ── Usage ──────────────────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
  err "Usage: $0 <demo-name>"
  err "Example: $0 manufacturing"
  exit 1
fi

DEMO="$1"
DEMO_DIR="$REPO/demos/$DEMO"

if ! demo_exists "$DEMO"; then
  err "Demo '$DEMO' not found — expected $DEMO_DIR/manifest.yaml"
  exit 1
fi

require_yq

# ── Result tracking ────────────────────────────────────────────────────────────
FAILS=()
WARNS=()
PASSES=()

pass_check() { local n="$1" msg="$2"; PASSES+=("$n|$msg"); }
fail_check() { local n="$1" msg="$2"; FAILS+=("$n|$msg"); }
warn_check() { local n="$1" msg="$2"; WARNS+=("$n|$msg"); }

# ── Check 1 — Collector cap ────────────────────────────────────────────────────
TOTAL=$(yq eval '.collectors.total' "$DEMO_DIR/manifest.yaml")
EDGE_COUNT=$(yq eval '.collectors.edges | length' "$DEMO_DIR/manifest.yaml")
GW_EXISTS=$(yq eval '.collectors.gateway | has("name")' "$DEMO_DIR/manifest.yaml")
if [[ "$GW_EXISTS" == "true" ]]; then GW_N=1; else GW_N=0; fi
ACTUAL=$(( EDGE_COUNT + GW_N ))
if (( TOTAL > 10 )); then
  fail_check "1. Collector cap (≤10)" "total=$TOTAL exceeds 10"
elif (( ACTUAL != TOTAL )); then
  fail_check "1. Collector cap (≤10)" "total=$TOTAL but gateway+edges count=$ACTUAL"
elif [[ "$GW_EXISTS" != "true" ]]; then
  fail_check "1. Collector cap (≤10)" "no gateway defined in manifest"
else
  pass_check "1. Collector cap (≤10)" "total=$TOTAL, gateway=1, edges=$EDGE_COUNT"
fi

# ── Check 2 — Three signals ────────────────────────────────────────────────────
METRICS_COUNT=$(yq eval '.signals.metrics | length' "$DEMO_DIR/manifest.yaml")
LOGS_COUNT=$(yq eval '.signals.logs | length' "$DEMO_DIR/manifest.yaml")
TRACES_COUNT=$(yq eval '.signals.traces | length' "$DEMO_DIR/manifest.yaml")
COMPOSE="$DEMO_DIR/docker-compose.yaml"
SIGNAL_FAIL=""
for sig in metrics logs traces; do
  sims=$(yq eval ".signals.${sig}[]" "$DEMO_DIR/manifest.yaml" 2>/dev/null || true)
  if [[ -z "$sims" ]]; then
    SIGNAL_FAIL="$SIGNAL_FAIL ${sig}=empty"
  else
    while IFS= read -r sim; do
      [[ -z "$sim" ]] && continue
      if [[ ! -f "$COMPOSE" ]]; then
        SIGNAL_FAIL="$SIGNAL_FAIL docker-compose.yaml-not-found"
        break 2
      fi
      if ! yq eval ".services | has(\"$sim\")" "$COMPOSE" 2>/dev/null | grep -q "true"; then
        SIGNAL_FAIL="$SIGNAL_FAIL ${sim}-not-in-compose"
      fi
    done <<< "$sims"
  fi
done
if [[ -n "$SIGNAL_FAIL" ]]; then
  fail_check "2. Three signals" "issues:$SIGNAL_FAIL"
else
  pass_check "2. Three signals" "metrics=$METRICS_COUNT, logs=$LOGS_COUNT, traces=$TRACES_COUNT sources; all in compose"
fi

# ── Check 3 — Dynatrace destination ───────────────────────────────────────────
# New format: destinations.yaml uses managed type: dynatrace_otlp (not raw OTLP exporter).
# The managed dynatrace_otlp destination handles OTLP delivery and delta temporality internally.
# Check that: type is dynatrace_otlp, credentials use env-var placeholders (not literals),
# and telemetry_types covers all three signals (Metrics, Logs, Traces).
DEST="$DEMO_DIR/bindplane/destinations.yaml"
if [[ ! -f "$DEST" ]]; then
  fail_check "3. Dynatrace destination" "bindplane/destinations.yaml not found"
else
  # Strip comments (# to EOL) before matching
  DEST_BODY=$(sed 's/#.*//' "$DEST" 2>/dev/null || true)
  HAS_DT_OTLP=$(printf '%s\n' "$DEST_BODY" | grep -cE 'type:[[:space:]]*dynatrace_otlp' || true)
  HAS_ENV_PLACEHOLDER=$(printf '%s\n' "$DEST_BODY" | grep -cE 'your_environment_id|dynatrace_api_token' || true)
  HAS_LITERAL=$(printf '%s\n' "$DEST_BODY" | grep -cE 'dt0c01\.[A-Za-z0-9]+|dt0s16\.[A-Za-z0-9]+' || true)
  HAS_METRICS=$(printf '%s\n' "$DEST_BODY" | grep -ciE '[[:space:]-][[:space:]]*Metrics' || true)
  HAS_LOGS=$(printf '%s\n' "$DEST_BODY" | grep -ciE '[[:space:]-][[:space:]]*Logs' || true)
  HAS_TRACES=$(printf '%s\n' "$DEST_BODY" | grep -ciE '[[:space:]-][[:space:]]*Traces' || true)
  DEST_FAIL=""
  (( HAS_DT_OTLP == 0 ))         && DEST_FAIL="$DEST_FAIL no-dynatrace_otlp-type"
  (( HAS_ENV_PLACEHOLDER == 0 )) && DEST_FAIL="$DEST_FAIL no-env-var-placeholders"
  (( HAS_LITERAL > 0 ))          && DEST_FAIL="$DEST_FAIL literal-token-found(FAIL)"
  (( HAS_METRICS == 0 ))         && DEST_FAIL="$DEST_FAIL telemetry_types-missing-Metrics"
  (( HAS_LOGS == 0 ))            && DEST_FAIL="$DEST_FAIL telemetry_types-missing-Logs"
  (( HAS_TRACES == 0 ))          && DEST_FAIL="$DEST_FAIL telemetry_types-missing-Traces"
  if [[ -n "$DEST_FAIL" ]]; then
    fail_check "3. Dynatrace destination" "$DEST_FAIL"
  else
    pass_check "3. Dynatrace destination" "dynatrace_otlp type, env-substituted credentials, telemetry_types includes Metrics+Logs+Traces"
  fi
fi

# ── Check 4 — Delta temporality ───────────────────────────────────────────────
# The managed dynatrace_otlp destination (check 3) handles delta temporality conversion
# internally — no cumulativetodelta processor is required in the pipeline.
# PASS if: destinations.yaml has dynatrace_otlp type (confirmed in check 3), OR
#           configurations.yaml or processors.yaml explicitly includes cumulativetodelta
#           (for demo setups that use a raw OTLP exporter instead of the managed destination).
DEST_FOR_DELTA="$DEMO_DIR/bindplane/destinations.yaml"
CFG_FOR_DELTA="$DEMO_DIR/bindplane/configurations.yaml"
PROC_FOR_DELTA="$DEMO_DIR/bindplane/processors.yaml"
DELTA_PASS=false
DELTA_DETAIL=""
# Path 1: managed dynatrace_otlp destination handles delta internally
if [[ -f "$DEST_FOR_DELTA" ]]; then
  DEST_BODY_DELTA=$(sed 's/#.*//' "$DEST_FOR_DELTA" 2>/dev/null || true)
  DT_OTLP_COUNT=$(printf '%s\n' "$DEST_BODY_DELTA" | grep -cE 'type:[[:space:]]*dynatrace_otlp' || true)
  if (( DT_OTLP_COUNT > 0 )); then
    DELTA_PASS=true
    DELTA_DETAIL="dynatrace_otlp destination handles delta temporality internally"
  fi
fi
# Path 2: explicit cumulativetodelta processor
if [[ "$DELTA_PASS" == "false" ]]; then
  HAS_CTD=false
  if [[ -f "$CFG_FOR_DELTA" ]] && grep -q 'cumulativetodelta' "$CFG_FOR_DELTA" 2>/dev/null; then
    HAS_CTD=true
  fi
  if [[ -f "$PROC_FOR_DELTA" ]] && grep -q 'cumulativetodelta' "$PROC_FOR_DELTA" 2>/dev/null; then
    HAS_CTD=true
  fi
  if [[ "$HAS_CTD" == "true" ]]; then
    DELTA_PASS=true
    DELTA_DETAIL="cumulativetodelta processor present in pipeline"
  fi
fi
if [[ "$DELTA_PASS" == "true" ]]; then
  pass_check "4. Delta temporality" "$DELTA_DETAIL"
else
  fail_check "4. Delta temporality" "neither dynatrace_otlp destination nor cumulativetodelta processor found — Dynatrace requires delta temporality"
fi

# ── Check 5 — Volume cap ──────────────────────────────────────────────────────
GB=$(yq eval '.caps.est_gb_per_day' "$DEMO_DIR/manifest.yaml")
INTERVAL=$(yq eval '.caps.scrape_interval_s' "$DEMO_DIR/manifest.yaml")
VOL_FAIL=""
if awk "BEGIN{exit !($GB >= 10)}"; then
  VOL_FAIL="est_gb_per_day=$GB >= 10 (cap exceeded)"
fi
if awk "BEGIN{exit !($INTERVAL < 30)}"; then
  VOL_FAIL="$VOL_FAIL scrape_interval_s=$INTERVAL < 30"
fi
VOL_FAIL="${VOL_FAIL# }"
if [[ -n "$VOL_FAIL" ]]; then
  fail_check "5. Volume cap (<10 GB/day)" "$VOL_FAIL"
elif awk "BEGIN{exit !($GB >= 8)}"; then
  warn_check "5. Volume cap (<10 GB/day)" "WARN: $GB GB/day is close to 10 GB cap, interval=${INTERVAL}s"
else
  pass_check "5. Volume cap (<10 GB/day)" "${GB} GB/day, interval=${INTERVAL}s"
fi

# ── Check 6 — Label ↔ selector agreement (2-config model) ─────────────────────
# Expected shape: exactly two Configurations per demo:
#   <demo>-gateway  → selector {role: gateway, demo: <demo>}
#   <demo>-edge     → selector {role: edge,    demo: <demo>}
#
# Rules:
#   a) Each collectors/*.env OPAMP_LABELS must be a superset of at least one
#      of those two selectors (subset matching: extra labels are fine).
#   b) BOTH selectors must be matched by at least one collector.
#
# Note: the "signal=" label is part of the OLD 3-config model. Under the new
# per-device unified model, edge collectors should NOT carry a signal= label;
# they match the edge configuration by role+demo alone.
COLLECTORS_DIR="$DEMO_DIR/collectors"
CFG="$DEMO_DIR/bindplane/configurations.yaml"
if [[ ! -d "$COLLECTORS_DIR" ]]; then
  fail_check "6. Label↔selector agreement" "collectors/ directory not found"
elif [[ ! -f "$CFG" ]]; then
  fail_check "6. Label↔selector agreement" "bindplane/configurations.yaml not found"
else
  LABEL_FAIL=""
  ENV_COUNT=0

  # Collect all OPAMP_LABELS values from *.env files
  shopt -s nullglob
  env_files=("$COLLECTORS_DIR"/*.env)
  shopt -u nullglob
  if (( ${#env_files[@]} == 0 )); then
    fail_check "6. Label↔selector agreement" "no *.env files found in collectors/"
  else
    for env_file in "${env_files[@]}"; do
      [[ -f "$env_file" ]] || continue
      (( ENV_COUNT++ ))
      LABELS_LINE=$(grep '^OPAMP_LABELS=' "$env_file" 2>/dev/null || true)
      if [[ -z "$LABELS_LINE" ]]; then
        LABEL_FAIL="$LABEL_FAIL $(basename "$env_file"):missing-OPAMP_LABELS"
      fi
    done

    # Build the two expected selectors for this demo
    GW_SELECTOR="role=gateway demo=$DEMO"
    EDGE_SELECTOR="role=edge demo=$DEMO"

    # Check each collector env matches at least one selector
    for env_file in "${env_files[@]}"; do
      [[ -f "$env_file" ]] || continue
      LABELS_VAL=$(grep '^OPAMP_LABELS=' "$env_file" 2>/dev/null | sed 's/^OPAMP_LABELS=//' || true)
      [[ -z "$LABELS_VAL" ]] && continue  # already flagged above
      MATCHED_ENV=false
      for sel in "$GW_SELECTOR" "$EDGE_SELECTOR"; do
        ALL_FOUND_E=true
        for kv in $sel; do
          if ! printf '%s' "$LABELS_VAL" | tr ',' '\n' | grep -qxF "$kv"; then
            ALL_FOUND_E=false
            break
          fi
        done
        if [[ "$ALL_FOUND_E" == "true" ]]; then
          MATCHED_ENV=true
          break
        fi
      done
      if [[ "$MATCHED_ENV" == "false" ]]; then
        LABEL_FAIL="$LABEL_FAIL $(basename "$env_file"):no-selector-match(expected-role=gateway,demo=$DEMO-or-role=edge,demo=$DEMO)"
      fi
    done

    # Check each selector is matched by at least one collector
    for sel in "$GW_SELECTOR" "$EDGE_SELECTOR"; do
      SEL_MATCHED=false
      for env_file in "${env_files[@]}"; do
        [[ -f "$env_file" ]] || continue
        LABELS_VAL=$(grep '^OPAMP_LABELS=' "$env_file" 2>/dev/null | sed 's/^OPAMP_LABELS=//' || true)
        [[ -z "$LABELS_VAL" ]] && continue
        ALL_FOUND_S=true
        for kv in $sel; do
          if ! printf '%s' "$LABELS_VAL" | tr ',' '\n' | grep -qxF "$kv"; then
            ALL_FOUND_S=false
            break
          fi
        done
        if [[ "$ALL_FOUND_S" == "true" ]]; then
          SEL_MATCHED=true
          break
        fi
      done
      if [[ "$SEL_MATCHED" == "false" ]]; then
        sel_str="$(printf '%s' "$sel" | tr ' ' ',')"
        LABEL_FAIL="$LABEL_FAIL selector{$sel_str}:no-collector-matches"
      fi
    done

    if [[ -n "$LABEL_FAIL" ]]; then
      fail_check "6. Label↔selector agreement" "$LABEL_FAIL"
    else
      pass_check "6. Label↔selector agreement" "$ENV_COUNT collector env files; both gateway+edge selectors matched"
    fi
  fi
fi

# ── Check 7 — Pinned image ────────────────────────────────────────────────────
BDOT_IMG=$(yq eval '.bdot_image' "$DEMO_DIR/manifest.yaml")
if [[ -z "$BDOT_IMG" || "$BDOT_IMG" == "null" ]]; then
  fail_check "7. Pinned image" "bdot_image not set in manifest"
elif echo "$BDOT_IMG" | grep -qE ':latest$'; then
  fail_check "7. Pinned image" "bdot_image uses :latest tag ($BDOT_IMG)"
else
  pass_check "7. Pinned image" "$BDOT_IMG"
fi

# ── Check 8 — No committed secrets ────────────────────────────────────────────
SECRET_FOUND=""
if grep -rE 'dt0c01\.[A-Za-z0-9]+|dt0s16\.[A-Za-z0-9]+' "$DEMO_DIR" 2>/dev/null \
    | grep -v '\.example' | grep -q .; then
  SECRET_FOUND="$SECRET_FOUND literal-dt-token-found"
fi
if grep -rE '^(DT_API_TOKEN|BP_SECRET_KEY)=.+' "$DEMO_DIR" 2>/dev/null \
    | grep -v '\.example' | grep -q .; then
  SECRET_FOUND="$SECRET_FOUND non-empty-secret-key-committed"
fi
SECRET_FOUND="${SECRET_FOUND# }"
if [[ -n "$SECRET_FOUND" ]]; then
  fail_check "8. No committed secrets" "$SECRET_FOUND"
else
  pass_check "8. No committed secrets" "no literal tokens found"
fi

# ── Print results table ────────────────────────────────────────────────────────
SEP="─────────────────────────────────────────────────────────────────────"

printf "\nValidating demo: %s\n" "$DEMO"
printf "%s\n" "$SEP"
printf " %-30s %-8s %s\n" "Check" "Result" "Details"
printf "%s\n" "$SEP"

# Note: "${ARR[@]+"${ARR[@]}"}" guards empty-array expansion under `set -u` on bash 3.2 (macOS).
for p in "${PASSES[@]+"${PASSES[@]}"}"; do
  name="${p%%|*}"; detail="${p#*|}"
  printf " %-30s %-8s %s\n" "$name" "PASS" "$detail"
done
for w in "${WARNS[@]+"${WARNS[@]}"}"; do
  name="${w%%|*}"; detail="${w#*|}"
  printf " %-30s %-8s %s\n" "$name" "WARN" "$detail"
done
for f in "${FAILS[@]+"${FAILS[@]}"}"; do
  name="${f%%|*}"; detail="${f#*|}"
  printf " %-30s %-8s %s\n" "$name" "FAIL" "$detail"
done

printf "%s\n" "$SEP"
if (( ${#FAILS[@]} == 0 )); then
  echo "READY TO DEPLOY ✓"
  exit 0
else
  echo "VALIDATION FAILED — fix the issues above before running up.sh"
  exit 1
fi
