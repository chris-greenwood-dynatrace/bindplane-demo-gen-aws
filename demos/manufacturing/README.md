# Manufacturing — Factory Machine Fleet Demo

This demo shows BindPlane managing a fleet of six OpenTelemetry collectors (BDOT) across a simulated factory: a site gateway plus five production-line edge collectors (Line A CNC mills, Line B CNC lathes, Line C assembly robots, Packaging, and Utilities). Each simulated machine emits **all three signals — metrics, logs, and traces — over OTLP with identical OTel Resource attributes** (`service.namespace=manufacturing`, `service.name=<machine-id>`, `host.name=<machine-id>`, `machine.id`, `machine.type`, `manufacturing.line`, `manufacturing.site=plant-1`), so every machine correlates into a **single Dynatrace entity** rather than three disconnected data streams. The `device-sim` container (one per line, configurable `DEVICE_COUNT` machines each) produces: **metrics** — gauges for temperature (°C), vibration (mm/s), spindle load (%), power (kW), cycle time (s), and OEE availability/performance/quality/overall, plus cumulative counters for parts completed, defects, and downtime; **logs** — realistic machine events at varied severity (startup INFO, maintenance-due WARN, over-temp WARN, servo-fault ERROR, jam ERROR) exported via the OTel Logs SDK; **traces** — a `production_cycle` parent span with `load_part` → `machining` → `inspect` → `unload` child spans, with configurable fault injection (`FAULT_RATE`) that sets `ERROR` status and records exceptions on the machining span. Edge collectors receive all three signals over OTLP gRPC and forward them to the gateway, which holds the Dynatrace token and exports via the `dynatrace_otlp` destination managed by BindPlane. The live demo moment is a fleet-wide **BindPlane Rollout**: add a `cost_center` transform processor to the gateway configuration in the UI, click Rollout, and watch all collectors converge with zero downtime while the new attribute appears in Dynatrace within seconds. **All telemetry is simulator-generated; no real factory equipment is required.** The demo is reusable across manufacturing verticals (automotive, food & beverage, pharma, electronics, metals) by relabeling machine types and line names in `.env.demo` — the OTel pipeline and BindPlane configurations need no changes.

## Business variants

| Variant | Line relabeling | Machine types |
|---------|----------------|---------------|
| `automotive` | body-shop, paint, assembly | stamping-press, robot-welder, trim-line |
| `food_bev` | mixing, filling, packaging | blender, filler, capper, labeler |
| `pharma` | synthesis, QC, packaging | reactor, centrifuge, tablet-press (GMP batch IDs in span attrs) |
| `electronics` | SMT, wave-solder, test | pick-and-place, reflow-oven, AOI |
| `metals` | melt, rolling, finishing | furnace, roller, press |

All variants share the same metric names, log structure, trace workflow, and BindPlane configurations. Change `MACHINE_TYPE` and `LINE_NAME` in the compose environment to adapt.

## Spin up

```bash
scripts/up.sh --demo manufacturing
```

See `bindplane/rollout.md` for the BindPlane UI steps after spin-up.
