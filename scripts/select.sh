#!/usr/bin/env bash
# scripts/select.sh — interactive demo picker; echoes chosen demo name to stdout.
set -euo pipefail

# shellcheck source=scripts/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

require_yq

# ── Collect demos (skip _template and any dir starting with _) ────────────────
mapfile -t MANIFESTS < <(
  find "$REPO/demos" -mindepth 2 -maxdepth 2 -name "manifest.yaml" \
    | sort \
    | grep -v '/demos/_'
)

if [[ ${#MANIFESTS[@]} -eq 0 ]]; then
  err "No demos found in $REPO/demos/"
  exit 1
fi

# Build parallel arrays: DEMO_NAMES and DEMO_LABELS
DEMO_NAMES=()
DEMO_LABELS=()
for manifest in "${MANIFESTS[@]}"; do
  demo_dir="$(dirname "$manifest")"
  name="$(basename "$demo_dir")"
  display="$(yq eval '.display_name // .name' "$manifest")"
  DEMO_NAMES+=("$name")
  DEMO_LABELS+=("$display")
done

# ── Auto-select if only one demo ─────────────────────────────────────────────
if [[ ${#DEMO_NAMES[@]} -eq 1 ]]; then
  printf 'Auto-selecting only demo: %s\n' "${DEMO_NAMES[0]}" >&2
  echo "${DEMO_NAMES[0]}"
  exit 0
fi

# ── Print numbered list to stderr (stdout stays clean) ───────────────────────
printf '\nAvailable demos:\n' >&2
# Determine max name width for alignment
max_len=0
for name in "${DEMO_NAMES[@]}"; do
  (( ${#name} > max_len )) && max_len=${#name}
done

for i in "${!DEMO_NAMES[@]}"; do
  num=$(( i + 1 ))
  printf '  %d) %-*s — %s\n' "$num" "$max_len" "${DEMO_NAMES[$i]}" "${DEMO_LABELS[$i]}" >&2
done
printf '\n' >&2

# ── Read user choice from /dev/tty (works even when stdout is captured) ───────
trap 'printf "\nAborted.\n" >&2; exit 130' INT

while true; do
  printf 'Select demo [1-%d]: ' "${#DEMO_NAMES[@]}" >&2
  if ! read -r choice < /dev/tty; then
    printf '\nAborted.\n' >&2
    exit 130
  fi

  # Validate: must be an integer in range
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#DEMO_NAMES[@]} )); then
    break
  fi
  warn "Invalid choice '$choice'. Enter a number between 1 and ${#DEMO_NAMES[@]}."
done

# ── Echo the name to stdout (captured by callers via DEMO=$(select.sh)) ──────
echo "${DEMO_NAMES[$(( choice - 1 ))]}"
