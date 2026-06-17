#!/usr/bin/env bash
# scripts/demos.sh — list available demos
set -euo pipefail

# shellcheck source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# ── cmd_list ──────────────────────────────────────────────────────────────────
cmd_list() {
  require_yq

  local manifests=()
  # Collect all manifest.yaml files under demos/
  while IFS= read -r -d '' f; do
    manifests+=("$f")
  done < <(find "$REPO/demos" -maxdepth 2 -name "manifest.yaml" -print0 2>/dev/null | sort -z)

  if [[ ${#manifests[@]} -eq 0 ]]; then
    info "No demos found under $REPO/demos/"
    return
  fi

  local col_name=20
  local col_display=40
  local col_collectors=10

  # Header
  printf '%-*s  %-*s  %s\n' \
    "$col_name" "NAME" \
    "$col_display" "DISPLAY NAME" \
    "COLLECTORS"
  printf '%s\n' "$(printf '%.0s-' {1..75})"

  local found=0
  for manifest in "${manifests[@]}"; do
    local demo_dir
    demo_dir="$(dirname "$manifest")"
    local demo_name
    demo_name="$(basename "$demo_dir")"

    # Skip entries whose directory name starts with underscore (e.g. _template)
    [[ "$demo_name" == _* ]] && continue

    local name display_name collectors
    name="$(yq eval '.name // ""' "$manifest")"
    display_name="$(yq eval '.display_name // ""' "$manifest")"
    collectors="$(yq eval '.collectors.total // "?"' "$manifest")"

    # Fall back to directory name if .name is empty
    [[ -z "$name" ]] && name="$demo_name"

    printf '%-*s  %-*s  %s\n' \
      "$col_name" "$name" \
      "$col_display" "$display_name" \
      "$collectors"

    (( found++ )) || true
  done

  if [[ "$found" -eq 0 ]]; then
    info "No demos found (all entries skipped)."
  fi
}

# ── usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $(basename "$0") [COMMAND]

Commands:
  list    List all available demos (default)

Options:
  -h, --help   Show this help message
EOF
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
  local cmd="${1:-list}"

  case "$cmd" in
    list)
      cmd_list
      ;;
    -h|--help)
      usage
      ;;
    *)
      warn "Unknown command: $cmd"
      usage
      exit 1
      ;;
  esac
}

main "$@"
