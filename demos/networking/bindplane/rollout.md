# BindPlane Rollout — Networking Demo

> **API-applied pipelines.** `scripts/up.sh` calls `bindplane apply` automatically after the VM is
> up. You do NOT need to build anything in the BindPlane UI. Collectors enroll via OpAMP, BindPlane
> matches them by label, and the pipelines are pushed within ~60 seconds.
> **All telemetry in this demo is simulator-generated** — no real network devices or NOC systems.

## What up.sh does automatically

1. Terraform provisions the Azure VM and writes `/opt/demo/.env` (contains `DT_ENV_ID`,
   `DT_API_TOKEN`, `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`).
2. Docker Compose starts all 5 BDOT collectors + simulators on the VM. Collectors enroll to
   BindPlane Cloud over OpAMP immediately.
3. `bindplane apply -f bindplane/destinations.yaml` creates (or updates) the two Destination
   resources: `dynatrace-networking` and `gateway-otlp-networking`.
4. `bindplane apply -f bindplane/configurations.yaml` creates (or updates) the two Configuration
   resources. BindPlane immediately begins pushing the matching pipeline to each enrolled collector.
5. `bindplane rollout start networking-gateway` and `bindplane rollout start networking-edge`
   trigger rollouts for each Configuration (logged as a warning and skipped if already current).

## Prerequisites (before running up.sh)

- [ ] `.env` in repo root contains: `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`, `BP_API_KEY`,
      `BP_ORG_ID`, `DT_ENV_ID`, `DT_API_TOKEN`, `SSH_PUBLIC_KEY_PATH`, `AZURE_LOCATION`
- [ ] Dynatrace API token scopes: `metrics.ingest`, `logs.ingest`, `openTelemetry.ingest`
- [ ] Azure credentials active (`az login` or ARM env vars set)

## Verify after up.sh completes

1. Open BindPlane UI: https://app.bindplane.com
2. Go to **Agents** and filter by `demo=networking`. All 5 collectors should appear
   **Connected** within ~60 seconds of the VM starting.

Expected collector to configuration assignments:

| Collector        | Configuration      | Selector match                   |
|------------------|--------------------|----------------------------------|
| gateway          | networking-gateway | role=gateway, demo=networking    |
| site-core        | networking-edge    | role=edge, demo=networking       |
| site-edge        | networking-edge    | role=edge, demo=networking       |
| firewalls        | networking-edge    | role=edge, demo=networking       |
| loadbalancers    | networking-edge    | role=edge, demo=networking       |

3. If any collector shows **Config Pending** after 2 minutes: in the UI, open the Configuration
   and click **Rollout** to force a push.

## Verify telemetry in Dynatrace

Open `https://<DT_ENV_ID>.live.dynatrace.com`

**Metrics** → Metrics Explorer:
- Search: `net.interface.in.octets`, `net.interface.out.octets`, `device.cpu.utilization`
- Filter by `devgroup` = core / edge / fw / lb
- Also: `firewall.sessions.active`, `lb.connections.active`, `net.latency.rtt`

**Logs** → Log Viewer:
- Filter: `device.id` contains `core-` / `edge-` / `fw-` / `lb-`
- Look for: LINEPROTO up/down, BGP-5-ADJCHANGE, SEC-6-IPACCESSLOGP ACL deny, OSPF adjacency events

**Distributed Traces** → Trace search:
- Service: `net-provisioning`
- Root spans: `receive_change_request` with children `validate_config`, `reserve_resources`,
  `push_to_devices` (device-level children: connect/apply/commit), `verify_connectivity`,
  `complete` / `rollback`

**If telemetry is missing:**
- Run `scripts/logs.sh --demo networking` to tail collector logs on the VM.
- Confirm all 5 collectors show **Connected** (not just gateway).
- Verify `DT_ENV_ID` and `DT_API_TOKEN` are correct in `/opt/demo/.env` on the VM
  (`ssh <vm> sudo cat /opt/demo/.env`).

---

## Live Demo Change — the "wow moment"

The pipeline is already running. The live demo shows BindPlane pushing a pipeline change
fleet-wide in real time, with zero SSH and zero restarts.

### Option A — Filter out info-severity syslog (volume reduction)

**Scenario:** a NOC policy suppresses chatty ACL-deny / informational syslog to reduce
log ingest cost. This is the highest-impact demo for a NOC audience.

1. In BindPlane UI → **Configurations** → `networking-gateway`
2. Click **Edit**. On the `otlp` source (logs pipeline), click **Add Processor**.
3. Choose **Filter Severity**. Configure:
   - Action: drop logs where `severity < WARNING`
4. Click **Save** — BindPlane displays the pending config diff.
5. Click **Rollout** → choose **Progressive** (1 agent first, then all).

**Talking points while the UI updates:**
- "Watch BindPlane push the new pipeline to the gateway — no SSH, no restart."
- "Log volume in Dynatrace drops immediately — no changes on edge collectors or the devices."
- "The filter decision is centralized at the gateway. One change, fleet-wide effect."
- "To roll back: hit Revert — same diff, same Rollout mechanism, same convergence view."

Verify in Dynatrace Log Viewer: count of log events drops; only Warning and above visible.

### Option B — Add site and cost-center attributes (attribute enrichment)

**Scenario:** a cost-allocation tagging policy is pushed to the NOC gateway without touching
any device or edge collector.

1. In BindPlane UI → **Configurations** → `networking-gateway`
2. Click **Edit**. On the `otlp` source, click **Add Processor**.
3. Choose **Transform**. Configure:
   - Telemetry type: `Metrics`
   - Statements (resource context):
     ```
     set(attributes["cost_center"], "NET-DC1-OPS")
     set(attributes["noc.site"], "dc-1")
     set(attributes["demo"], "networking")
     ```
4. Add the same Transform on the `Logs` pipeline.
5. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "In ~30 seconds, new metrics/logs in Dynatrace will carry `cost_center=NET-DC1-OPS`."
- "No SSH to any device. No collector restart. The pipeline change propagates via OpAMP."
- "Revert is one click — BindPlane shows the exact config diff both ways."

Verify in Dynatrace Metrics Explorer: filter by attribute `cost_center = NET-DC1-OPS`.
