# BindPlane Rollout — Manufacturing Demo

> **API-applied pipelines.** `scripts/up.sh` calls `bindplane apply` automatically after the VM is
> up. You do NOT need to build anything in the BindPlane UI. Collectors enroll via OpAMP, BindPlane
> matches them by label, and the pipelines are pushed within ~60 seconds.
> **All telemetry in this demo is simulator-generated** — no real machines or factory systems.

## What up.sh does automatically

1. Terraform provisions the Azure VM and writes `/opt/demo/.env` (contains `DT_ENV_ID`,
   `DT_API_TOKEN`, `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`).
2. Docker Compose starts all 6 BDOT collectors + simulators on the VM. Collectors enroll to
   BindPlane Cloud over OpAMP immediately.
3. `bindplane apply -f bindplane/destinations.yaml` creates (or updates) the two Destination
   resources: `dynatrace-manufacturing` and `gateway-otlp-manufacturing`.
4. `bindplane apply -f bindplane/configurations.yaml` creates (or updates) the two Configuration
   resources. BindPlane immediately begins pushing the matching pipeline to each enrolled collector.
5. `bindplane rollout start manufacturing-gateway` and `bindplane rollout start manufacturing-edge`
   trigger rollouts for each Configuration (logged as a warning and skipped if already current).

## Prerequisites (before running up.sh)

- [ ] `.env` in repo root contains: `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`, `BP_API_KEY`,
      `BP_ORG_ID`, `DT_ENV_ID`, `DT_API_TOKEN`, `SSH_PUBLIC_KEY_PATH`, `AZURE_LOCATION`
- [ ] Dynatrace API token scopes: `metrics.ingest`, `logs.ingest`, `openTelemetry.ingest`
- [ ] Azure credentials active (`az login` or ARM env vars set)

## Verify after up.sh completes

1. Open BindPlane UI: https://app.bindplane.com
2. Go to **Agents** and filter by `demo=manufacturing`. All 6 collectors should appear
   **Connected** within ~60 seconds of the VM starting.

Expected collector to configuration assignments:

| Collector  | Configuration            | Selector match                      |
|------------|--------------------------|-------------------------------------|
| gateway    | manufacturing-gateway    | role=gateway, demo=manufacturing    |
| line-a     | manufacturing-edge       | role=edge, demo=manufacturing       |
| line-b     | manufacturing-edge       | role=edge, demo=manufacturing       |
| line-c     | manufacturing-edge       | role=edge, demo=manufacturing       |
| packaging  | manufacturing-edge       | role=edge, demo=manufacturing       |
| utilities  | manufacturing-edge       | role=edge, demo=manufacturing       |

3. If any collector shows **Config Pending** after 2 minutes: in the UI, open the Configuration
   and click **Rollout** to force a push.

## Verify telemetry in Dynatrace

Open `https://<DT_ENV_ID>.live.dynatrace.com`

**Metrics** → Metrics Explorer:
- Search: `machine.temperature`, `oee.overall`, `production.parts_completed`
- Filter by resource attribute: `manufacturing.line` = A / B / C / pkg / util

**Logs** → Log Viewer:
- Filter: `service.name` contains `line-` or `host.name` = machine IDs
- Severity range: info (tool change, maintenance) → warning → error → critical (crash)

**Distributed Traces** → Trace search:
- Service: `mes-control`
- Root spans: `receive_order` with children `schedule_job`, `assign_machine`, `run_job`,
  `quality_check`, `complete` / `ship` / `error_rework`

**If telemetry is missing:**
- Run `scripts/logs.sh --demo manufacturing` to tail collector logs on the VM.
- Confirm all 6 collectors show **Connected** (not just gateway).
- Verify `DT_ENV_ID` and `DT_API_TOKEN` are correct in `/opt/demo/.env` on the VM
  (`ssh <vm> sudo cat /opt/demo/.env`).

---

## Live Demo Change — the "wow moment"

The pipeline is already running. The live demo shows BindPlane pushing a pipeline change
fleet-wide in real time, with zero SSH and zero restarts.

### Option A — Add a cost-center resource attribute (attribute enrichment)

**Scenario:** a cost-allocation tagging policy is being pushed to the entire factory fleet.

1. In BindPlane UI → **Configurations** → `manufacturing-gateway`
2. Click **Edit**. On the `otlp` source, click **Add Processor**.
3. Choose **Transform**. Configure:
   - Telemetry type: `Metrics`
   - Statements (resource context):
     ```
     set(attributes["cost_center"], "MFG-PLANT1-OPS")
     set(attributes["demo"], "manufacturing")
     ```
4. Add the same Transform on the `Logs` pipeline.
5. Click **Save** — BindPlane displays the pending config diff.
6. Click **Rollout** → choose **Progressive** (1 agent first, then all).

**Talking points while the UI updates:**
- "Watch BindPlane push the new pipeline to the gateway — no SSH, no restart."
- "The gateway hot-reloads its pipeline; existing OTLP connections from edge collectors stay up."
- "In ~30 seconds, new data points in Dynatrace will carry `cost_center=MFG-PLANT1-OPS`."
- "To roll back: hit Revert — same diff, same Rollout mechanism, same convergence view."

Verify in Dynatrace Metrics Explorer: filter by attribute `cost_center = MFG-PLANT1-OPS`.

### Option B — Drop info-severity logs (volume reduction / filter policy)

**Scenario:** suppress low-severity machine maintenance chatter to reduce log ingest volume.

1. In BindPlane UI → **Configurations** → `manufacturing-gateway`
2. Click **Edit**. On the `otlp` source (logs pipeline), click **Add Processor**.
3. Choose **Filter Severity**. Configure:
   - Action: drop logs where `severity < WARNING`
4. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "This simulates a log volume policy — only warning and above reach Dynatrace."
- "Log volume drops immediately. No changes on edge collectors."
- "The filter decision is centralized at the gateway — one place to change, fleet-wide effect."
