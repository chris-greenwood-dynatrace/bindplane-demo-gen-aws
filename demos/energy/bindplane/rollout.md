# BindPlane Rollout — Energy Demo

> **API-applied pipelines.** `scripts/up.sh` calls `bindplane apply` automatically after the VM is
> up. You do NOT need to build anything in the BindPlane UI. Collectors enroll via OpAMP, BindPlane
> matches them by label, and the pipelines are pushed within ~60 seconds.
> **All telemetry in this demo is simulator-generated** — no real grid assets, SCADA, or AMI data.

## What up.sh does automatically

1. Terraform provisions the Azure VM and writes `/opt/demo/.env` (contains `DT_ENV_ID`,
   `DT_API_TOKEN`, `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`).
2. Docker Compose starts all 7 BDOT collectors + simulators on the VM. Collectors enroll to
   BindPlane Cloud over OpAMP immediately.
3. `bindplane apply -f bindplane/destinations.yaml` creates (or updates) the two Destination
   resources: `dynatrace-energy` and `gateway-otlp-energy`.
4. `bindplane apply -f bindplane/configurations.yaml` creates (or updates) the two Configuration
   resources. BindPlane immediately begins pushing the matching pipeline to each enrolled collector.
5. `bindplane rollout start energy-gateway` and `bindplane rollout start energy-edge`
   trigger rollouts for each Configuration (logged as a warning and skipped if already current).

## Prerequisites (before running up.sh)

- [ ] `.env` in repo root contains: `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`, `BP_API_KEY`,
      `DT_OTLP_ENDPOINT`, `DT_API_TOKEN`, `SSH_PUBLIC_KEY_PATH`, `AZURE_LOCATION`
- [ ] Dynatrace API token scopes: `metrics.ingest`, `logs.ingest`, `openTelemetryTrace.ingest`
      (Gen 3 / OpenPipeline equivalents: `openpipeline:metrics:ingest`,
      `openpipeline:logs:ingest`, `openpipeline:events:ingest`)
- [ ] Azure credentials active (`az login` or ARM env vars set)

## Verify after up.sh completes

1. Open BindPlane UI: https://app.bindplane.com
2. Go to **Agents** and filter by `demo=energy`. All 7 collectors should appear
   **Connected** within ~60 seconds of the VM starting.

Expected collector to configuration assignments:

| Collector     | Configuration   | Selector match               |
|---------------|-----------------|------------------------------|
| gateway       | energy-gateway  | role=gateway, demo=energy    |
| substations   | energy-edge     | role=edge, demo=energy       |
| transformers  | energy-edge     | role=edge, demo=energy       |
| feeders       | energy-edge     | role=edge, demo=energy       |
| meters        | energy-edge     | role=edge, demo=energy       |
| generation    | energy-edge     | role=edge, demo=energy       |
| scada         | energy-edge     | role=edge, demo=energy       |

3. If any collector shows **Config Pending** after 2 minutes: in the UI, open the Configuration
   and click **Rollout** to force a push.

## Verify telemetry in Dynatrace

Open `https://<DT_ENV_ID>.live.dynatrace.com`

**Metrics** → Metrics Explorer:
- Search: `energy.substation.voltage_kv`, `energy.transformer.oil_temp_c`,
  `energy.feeder.load_amps`, `energy.gen.output_mw`, `energy.battery.soc_pct`,
  `energy.meter.online_count`, `energy.scada.poll_latency_ms`
- Filter by `energy.assetgroup` = substation / transformer / feeder / meter / gen / scada
- Filter by `energy.region` = region-east-1

**Logs** → Log Viewer:
- Filter: `asset.id` contains `sub-` / `xfmr-` / `fdr-` / `ami-` / `gen-` / `rtu-`
- Look for: SEL-RELAY-TRIP, BREAKER-OPEN, OIL-TEMP-HIGH, LTC-TAP-RAISE, RECLOSER-OP,
  AMI-COMM-FAIL, GEN-CURTAILMENT, RTU-HEARTBEAT-LOST, DNP3-UNSOL events

**Distributed Traces** → Trace search:
- Service namespace: `energy`
- Root spans: `grid_operation` with `operation.type` ∈
  {`fault_isolation`, `load_shed`, `restoration`, `dispatch`}
- Children: `detect`, `isolate`, `dispatch`, `verify`, optional `rollback`

**If telemetry is missing:**
- Run `scripts/logs.sh --demo energy` to tail collector logs on the VM.
- Confirm all 7 collectors show **Connected** (not just gateway).
- Verify `DT_OTLP_ENDPOINT` and `DT_API_TOKEN` are correct in `/opt/demo/.env` on the VM
  (`ssh <vm> sudo cat /opt/demo/.env`).

---

## Live Demo Change — the "wow moment"

The pipeline is already running. The live demo shows BindPlane pushing a pipeline change
fleet-wide in real time, with zero SSH and zero restarts.

### Option A — Filter out info-severity device chatter (volume reduction)

**Scenario:** a control-center policy suppresses chatty informational SCADA poll-success and
DNP3 unsolicited responses to reduce log ingest cost. This is the highest-impact demo for a
utility audience facing growing AMI/SCADA log volume.

1. In BindPlane UI → **Configurations** → `energy-gateway`
2. Click **Edit**. On the `otlp` source (logs pipeline), click **Add Processor**.
3. Choose **Filter Severity**. Configure:
   - Action: drop logs where `severity < WARNING`
4. Click **Save** — BindPlane displays the pending config diff.
5. Click **Rollout** → choose **Progressive** (1 agent first, then all).

**Talking points while the UI updates:**
- "Watch BindPlane push the new pipeline to the gateway — no SSH, no restart on the RTU
  concentrator, no field truck roll."
- "Log volume in Dynatrace drops immediately — only Warning+ events from substations,
  transformers, feeders, meters, generation, and SCADA."
- "The filter decision is centralized at the gateway. One change, fleet-wide effect across
  every assetgroup."
- "To roll back: hit Revert — same diff, same Rollout mechanism, same convergence view."

Verify in Dynatrace Log Viewer: count of log events drops; only Warning and above visible.

### Option B — Add region and cost-center attributes (attribute enrichment)

**Scenario:** a regulatory cost-allocation policy is pushed to the control-center gateway
without touching any substation, AMI head-end, generation asset, or SCADA RTU.

1. In BindPlane UI → **Configurations** → `energy-gateway`
2. Click **Edit**. On the `otlp` source, click **Add Processor**.
3. Choose **Transform**. Configure:
   - Telemetry type: `Metrics`
   - Statements (resource context):
     ```
     set(attributes["cost_center"], "T&D-EAST-OPS")
     set(attributes["energy.balancing_authority"], "PJM")
     set(attributes["demo"], "energy")
     ```
4. Add the same Transform on the `Logs` pipeline.
5. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "In ~30 seconds, new metrics and logs in Dynatrace will carry `cost_center=T&D-EAST-OPS`
  and `energy.balancing_authority=PJM`."
- "No SSH to any RTU. No collector restart. The pipeline change propagates via OpAMP."
- "Revert is one click — BindPlane shows the exact config diff both ways."

Verify in Dynatrace Metrics Explorer: filter by attribute `cost_center = T&D-EAST-OPS`.
