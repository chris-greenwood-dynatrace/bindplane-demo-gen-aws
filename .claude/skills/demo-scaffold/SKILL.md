---
name: demo-scaffold
description: Generate a new demos/<name>/ directory from demos/_template/ for the bindplane-demo repo, pre-filled to satisfy the demo contract (manifest with ≤10 collectors + labels + signal map, stub simulators, stub bindplane blueprints, rollout runbook). Use when the user wants to add a new BindPlane demo (e.g. "scaffold a demo for healthcare/retail/energy", "add demo C", "/demo-scaffold <name>").
---

# demo-scaffold

Creates a new self-contained demo that the existing Terraform + scripts pick up with **zero**
Terraform change (selection is convention-driven via `demos/*/manifest.yaml`).

## Inputs
- `<name>` — kebab-case demo id (e.g. `healthcare`, `energy-grid`). Becomes `demos/<name>/`.
- The domain/story (what devices/machines, which business variants, what each signal represents).

## Procedure
1. Read `/Users/clinton.smith/code/bindplane-demo/CLAUDE.md` (the demo contract + non-negotiable rules).
2. `cp -r demos/_template demos/<name>` (or recreate the structure if `_template` is absent).
3. Fill **manifest.yaml**:
   - `name`, `display_name`, `business_variants`.
   - `collectors.total` ≤ 10, with one `gateway` (labels `role=gateway, demo=<name>`) + edges
     (`role=edge` + a refining label like `signal`/`line`/`devgroup`).
   - `signals.{metrics,logs,traces}` — each non-empty, each naming a simulator.
   - `caps.est_gb_per_day` < 10, `scrape_interval_s` 30–60.
   - `bdot_image` pinned (copy the tag used by existing demos; never `:latest`).
4. Create **collectors/<name>.env** per collector with `OPAMP_LABELS=` matching the manifest.
5. Stub **simulators/** so all three signals are produced (delegate detail to the `otel-simulator`
   agent): a metrics source, a logs generator, and an OTel-instrumented FastAPI trace app.
6. Stub **docker-compose.yaml** (delegate to `otel-simulator`): BDOT per collector + simulators + app.
7. Stub **bindplane/** blueprints + **rollout.md** (delegate to the `bindplane-pipeline` agent):
   Dynatrace OTLP http/protobuf destination + cumulativetodelta; label-matched gateway/edge configs
   whose selectors equal the collectors' `OPAMP_LABELS`.
8. Write **README.md** (one paragraph: what it shows + variants).
9. Run the **bindplane-validate** skill against the new demo. Fix any failure before finishing.

## Output
Report the created tree, the collector count (must be ≤10), and confirm `bindplane-validate` passed.
Remind the operator that BindPlane pipelines are built once in the UI (free plan, no API) using
`bindplane/rollout.md`.
