# Energy Demo — Grid Substation & Generation Fleet

This demo shows BindPlane managing seven OpenTelemetry collectors monitoring a simulated electric utility grid: six edge collectors (substations, transformers, feeders, meters, generation, scada) each receive metrics, logs, and traces from a unified `device-sim` container running in their asset group, which simulates DEVICE_COUNT assets per group (HV substations/SEL, distribution transformers/ABB, feeders+reclosers/S&C, AMI smart-meter concentrators/Itron, generation assets including solar/wind/battery/gas peaker/GE, and SCADA RTUs/Hitachi) — every asset emits all three signals over OTLP gRPC with identical resource attributes (`service.name`, `host.name`, `asset.id`, `asset.type`, `asset.vendor`, `energy.assetgroup`, `energy.region`, `energy.voltage_class_kv`, plus `energy.gen.subtype` for generation) so Dynatrace correlates metrics, logs, and traces into a single entity per asset without any join configuration; metrics include grid-physics signals (substation voltage_kv / current_amps / frequency_hz / breaker_status / relay_trip_count), transformer thermal model (oil_temp_c, winding_temp_c, load_pct, ltc_tap_position, oil_level_pct), feeder operations (load_amps, voltage_deviation_pct, recloser_operations, momentary_outages, capbank_kvar), AMI fleet KPIs (online_count, outage_count, ami_read_success_rate, peak_demand_kw), generation KPIs (output_mw, availability_pct, battery soc_pct, solar irradiance_wm2, wind speed_mps), and SCADA health (rtu_online, poll_latency_ms, command_success_rate, comm_link_status); logs are utility-style structured records covering relay trips (SEL), breaker open/close, GOOSE timeouts, oil/winding temp warnings, LTC tap raise/lower, recloser operations, voltage sags/swells, AMI comm failures, meter outage notifications, generator starts/trips, inverter faults, battery cycles, RTU heartbeat losses, and DNP3 unsolicited responses at varied severities; traces model multi-span `grid_operation` workflows (`detect → isolate → dispatch → verify`, with fault-injected rollback paths keyed by `FAILURE_RATE`) across operation types (`fault_isolation`, `load_shed`, `restoration`, `dispatch`) so Dynatrace Distributed Traces show end-to-end control-action latency and failure rate per asset group; the gateway collector receives all signals forwarded by the six edge collectors and is the sole holder of the Dynatrace destination; all seven BDOT collectors enroll to BindPlane Cloud over OpAMP and receive their pipelines automatically via `bp-apply.sh`. The demo is reusable across business variants (investor-owned utility, electric co-op, municipal utility, T&D-only operator, ISO/RTO control-center) by adjusting asset_vendor and cost-center attributes; `DEVICE_COUNT`, `SCRAPE_INTERVAL_S`, `FAILURE_RATE`, and `INTENSITY` in `.env.demo` let you dial volume up or down to stay under the 10 GB/day free-plan cap.

## Files

- `manifest.yaml` — single source of truth (collectors, signals, caps, image)
- `docker-compose.yaml` — 7 BDOT collectors + 6 device-sim services on the `grid` network
- `.env.demo` — non-secret tuning knobs (DEVICE_COUNT, FAILURE_RATE, INTENSITY)
- `collectors/*.env` — per-collector `OPAMP_LABELS` (subset matched to BindPlane Configurations)
- `simulators/device-sim/` — unified Python OTel simulator (one image, six service instances)
- `bindplane/destinations.yaml` — managed `dynatrace_otlp` destination + edge→gateway OTLP gRPC
- `bindplane/configurations.yaml` — `energy-gateway` and `energy-edge` Configurations
- `bindplane/fleets.yaml` — Fleets pairing each Configuration with its role-keyed selector
- `bindplane/rollout.md` — verify checklist + live "wow moment" demo runbook

## Run

```bash
scripts/up.sh --demo energy
scripts/logs.sh --demo energy        # tail collector + simulator logs on the VM
scripts/down.sh --demo energy        # destroy infra (atomic)
```

## Talking points

- "BindPlane is managing seven OpenTelemetry collectors covering an entire utility grid:
  substations, distribution transformers, feeders, AMI meter fleet, generation assets,
  and SCADA RTUs — under the free-plan cap of ten."
- "Every grid asset emits metrics, logs, and traces with identical OTel resource
  attributes, so Dynatrace shows one entity per asset and correlates incidents end-to-end."
- "When a fault is injected, you see the simulated relay trip in logs, the breaker_status
  and frequency_hz drop in metrics, and the `grid_operation` trace span flips to ERROR
  and rolls back — across the same `asset.id`."
- "I can change the pipeline shape — filter info logs, add a `cost_center=T&D-EAST-OPS`
  attribute, route an asset group somewhere else — and BindPlane pushes it fleet-wide
  over OpAMP in about thirty seconds, with no SSH and no field truck roll."

## Business variants

| Variant     | Suggested asset_vendor / region overrides                                         |
|-------------|-----------------------------------------------------------------------------------|
| `iou`       | Mixed vendors per group; multiple regions; cost_center=T&D-OPS                    |
| `coop`      | Single region; vendor=Cooper/SEL; cost_center=COOP-OPS                            |
| `municipal` | Vendor=ABB/SEL; cost_center=MUNI-OPS; smaller DEVICE_COUNT                        |
| `td_only`   | Drop generation collector (or set DEVICE_COUNT=0 on `device-gen`)                 |
| `iso_rto`   | Emphasize gen + scada groups; cost_center=ISO-MARKETS; balancing_authority=PJM    |
