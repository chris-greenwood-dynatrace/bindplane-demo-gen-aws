# BindPlane Rollout — <DEMO_NAME> Demo

> **API-applied pipelines.** `scripts/up.sh` calls `bindplane apply` automatically after the VM is
> up. You do NOT need to build anything in the BindPlane UI. Collectors enroll via OpAMP, BindPlane
> matches them by label, and the pipelines are pushed within ~60 seconds.
> **All telemetry in this demo is simulator-generated.**

## What up.sh does automatically

1. Terraform provisions the AWS EC2 instance and writes `/opt/demo/.env` (contains `DT_ENV_ID`,
   `DT_API_TOKEN`, `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`).
2. Docker Compose starts all collectors + simulators on the instance. Collectors enroll to
   BindPlane Cloud over OpAMP immediately.
3. `bindplane apply -f bindplane/destinations.yaml` creates (or updates) the two Destination
   resources: `dynatrace-<DEMO_NAME>` and `gateway-otlp-<DEMO_NAME>`.
4. `bindplane apply -f bindplane/configurations.yaml` creates (or updates) the two Configuration
   resources. BindPlane immediately begins pushing the matching pipeline to each enrolled collector.
5. `bindplane rollout start <DEMO_NAME>-gateway` and `bindplane rollout start <DEMO_NAME>-edge`
   trigger rollouts for each Configuration (logged as a warning and skipped if already current).

## Prerequisites (before running up.sh)

- [ ] `.env` in repo root contains: `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`, `BP_API_KEY`,
      `BP_ORG_ID`, `DT_ENV_ID`, `DT_API_TOKEN`, `SSH_PUBLIC_KEY_PATH`, `AWS_REGION`
- [ ] Dynatrace API token scopes: `metrics.ingest`, `logs.ingest`, `openTelemetry.ingest`
- [ ] AWS credentials active (`aws configure`, `AWS_PROFILE`, or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` set)

## Verify after up.sh completes

1. Open BindPlane UI: https://app.bindplane.com
2. Go to **Agents** and filter by `demo=<DEMO_NAME>`. All collectors should appear
   **Connected** within ~60 seconds of the VM starting.

Expected collector to configuration assignments:

| Collector           | Configuration          | Selector match                     |
|---------------------|------------------------|------------------------------------|
| gateway             | <DEMO_NAME>-gateway    | role=gateway, demo=<DEMO_NAME>     |
| (each edge collector) | <DEMO_NAME>-edge     | role=edge, demo=<DEMO_NAME>        |

3. If any collector shows **Config Pending** after 2 minutes: in the UI, open the Configuration
   and click **Rollout** to force a push.

## Verify telemetry in Dynatrace

Open `https://<DT_ENV_ID>.live.dynatrace.com`

- **Metrics** → Metrics Explorer: search for demo-specific metric names
- **Logs** → Log Viewer: filter by `demo=<DEMO_NAME>` resource attribute
- **Distributed Traces** → Trace search: filter by `service.name`

**If telemetry is missing:**
- Run `scripts/logs.sh --demo <DEMO_NAME>` to tail collector logs on the VM.
- Confirm all collectors show **Connected** (not just gateway).
- Verify `DT_ENV_ID` and `DT_API_TOKEN` are correct in `/opt/demo/.env` on the VM
  (`ssh <vm> sudo cat /opt/demo/.env`).

---

## Live Demo Change — the "wow moment"

The pipeline is already running. The live demo shows BindPlane pushing a pipeline change
fleet-wide in real time, with zero SSH and zero restarts.

### Option A — Add a resource attribute (attribute enrichment)

**Scenario:** a tagging / cost-allocation policy is pushed fleet-wide without touching any
edge collector or simulator.

1. In BindPlane UI → **Configurations** → `<DEMO_NAME>-gateway`
2. Click **Edit**. On the `otlp` source, click **Add Processor**.
3. Choose **Transform**. Add resource-context attribute statements, for example:
   ```
   set(attributes["cost_center"], "<COST_CENTER_VALUE>")
   set(attributes["demo"], "<DEMO_NAME>")
   ```
4. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "Watch BindPlane push the new pipeline to the gateway — no SSH, no restart."
- "In ~30 seconds, new data in Dynatrace carries the new attribute."
- "Revert is one click — same diff mechanism, same convergence view."

### Option B — Filter logs by severity (volume reduction)

**Scenario:** suppress low-severity log chatter to reduce ingest volume.

1. In BindPlane UI → **Configurations** → `<DEMO_NAME>-gateway`
2. Click **Edit**. On the `otlp` source (logs pipeline), click **Add Processor**.
3. Choose **Filter Severity**. Set action to drop logs below WARNING.
4. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "Log volume in Dynatrace drops immediately — no changes on edge collectors."
- "The filter decision is centralized at the gateway. One change, fleet-wide effect."
- "To roll back: hit Revert — BindPlane shows the exact config diff both ways."
