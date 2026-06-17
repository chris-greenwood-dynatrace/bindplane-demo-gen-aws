---
name: bindplane-validate
description: Static guardrail that validates a bindplane-demo demo before any Azure spend — checks collector cap (≤10), three-signal completeness, Dynatrace OTLP http/protobuf + cumulativetodelta, daily volume cap, label↔selector agreement, and pinned BDOT image. Use before spinning up infra, after scaffolding or editing a demo, or when the user asks to "validate the demo" / "check before deploy".
---

# bindplane-validate

Validates one or all `demos/<name>/` against the non-negotiable rules in
`/Users/clinton.smith/code/bindplane-demo/CLAUDE.md`. Read-only; reports PASS/FAIL per check. Prefer
the helper script if present: `scripts/validate.sh [<demo>]`; otherwise perform the checks manually
by reading the files.

## Checks (all must PASS)
1. **Collector cap** — `manifest.collectors.total` ≤ 10, and the count of `gateway` + `edges` entries
   equals `total`, and there is exactly one `role=gateway`.
2. **Three signals** — `manifest.signals.metrics`, `.logs`, `.traces` each non-empty, and each named
   simulator exists as a service in `docker-compose.yaml`.
3. **Dynatrace destination** — `bindplane/destinations.yaml` uses OTLP **http/protobuf**, endpoint
   `https://<envid>.live.dynatrace.com/api/v2/otlp`, `Authorization: Api-Token` header (env-substituted,
   not a literal token), telemetry_types include logs+metrics+traces.
4. **Delta temporality** — `cumulativetodelta` present on the metrics pipeline in
   `bindplane/processors.yaml` + referenced by the gateway configuration.
5. **Volume cap** — `manifest.caps.est_gb_per_day` < 10; warn if ≥ 8. `scrape_interval_s` ≥ 30.
6. **Label ↔ selector agreement** — every `collectors/*.env` `OPAMP_LABELS` set matches a selector in
   `bindplane/configurations.yaml`, and vice-versa (no orphan selector, no unrouted collector).
7. **Pinned image** — `manifest.bdot_image` is set and is NOT `:latest`.
8. **No committed secrets** — grep the demo dir for literal `dt0c01.`/`dt0s16.` tokens or non-empty
   `DT_API_TOKEN=`/`BP_SECRET_KEY=` in tracked files → FAIL if found.

## Output
A table of check → PASS/FAIL/WARN with the offending file:line for any failure, then a one-line
verdict (`READY TO DEPLOY` only if all PASS). Do not modify files — report only.
