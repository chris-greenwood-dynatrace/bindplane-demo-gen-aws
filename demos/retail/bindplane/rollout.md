# BindPlane Rollout — Retail Demo

> **API-applied pipelines.** `scripts/up.sh` calls `bindplane apply` automatically after the VM is
> up. You do NOT need to build anything in the BindPlane UI. Collectors enroll via OpAMP, BindPlane
> matches them by label, and the pipelines are pushed within ~60 seconds.
> **All telemetry in this demo is simulator-generated** — no real stores, POS terminals, payment
> gateways, refrigeration units, or e-commerce sites.

## What up.sh does automatically

1. Terraform provisions the Azure VM and writes `/opt/demo/.env` (contains `DT_OTLP_ENDPOINT`,
   `DT_API_TOKEN`, `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`).
2. Docker Compose starts all 7 BDOT collectors + simulators on the VM. Collectors enroll to
   BindPlane Cloud over OpAMP immediately.
3. `bindplane apply -f bindplane/destinations.yaml` creates (or updates) the two Destination
   resources: `dynatrace-retail` and `gateway-otlp-retail`.
4. `bindplane apply -f bindplane/configurations.yaml` creates (or updates) the two Configuration
   resources. BindPlane immediately begins pushing the matching pipeline to each enrolled collector.
5. `bindplane rollout start retail-gateway` and `bindplane rollout start retail-edge` trigger
   rollouts for each Configuration (logged as a warning and skipped if already current).

## Prerequisites (before running up.sh)

- [ ] `.env` in repo root contains: `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`, `BP_API_KEY`,
      `DT_OTLP_ENDPOINT`, `DT_API_TOKEN`, `SSH_PUBLIC_KEY_PATH`, `AZURE_LOCATION`
- [ ] Dynatrace API token scopes: `metrics.ingest`, `logs.ingest`, `openTelemetryTrace.ingest`
      (Gen 3 / OpenPipeline equivalents: `openpipeline:metrics:ingest`,
      `openpipeline:logs:ingest`, `openpipeline:events:ingest`)
- [ ] Azure credentials active (`az login` or ARM env vars set)

## Verify after up.sh completes

1. Open BindPlane UI: https://app.bindplane.com
2. Go to **Agents** and filter by `demo=retail`. All 7 collectors should appear
   **Connected** within ~60 seconds of the VM starting.

Expected collector to configuration assignments:

| Collector  | Configuration   | Selector match              |
|------------|-----------------|-----------------------------|
| gateway    | retail-gateway  | role=gateway, demo=retail   |
| pos        | retail-edge     | role=edge, demo=retail      |
| payments   | retail-edge     | role=edge, demo=retail      |
| inventory  | retail-edge     | role=edge, demo=retail      |
| fridge     | retail-edge     | role=edge, demo=retail      |
| network    | retail-edge     | role=edge, demo=retail      |
| digital    | retail-edge     | role=edge, demo=retail      |

3. If any collector shows **Config Pending** after 2 minutes: in the UI, open the Configuration
   and click **Rollout** to force a push.

## Verify telemetry in Dynatrace

Open `https://<DT_ENV_ID>.live.dynatrace.com`

**Metrics** → Metrics Explorer:
- POS: `retail.pos.txn_per_min`, `retail.pos.basket_usd`, `retail.pos.avg_scan_ms`,
  `retail.pos.printer_online`, `retail.pos.void_count`
- Payments: `retail.payment.auth_latency_ms`, `retail.payment.approval_rate`,
  `retail.payment.decline_rate`, `retail.payment.chargeback_count`, `retail.payment.amount_usd`
- Inventory: `retail.inventory.scan_per_min`, `retail.inventory.scan_failure_rate`,
  `retail.inventory.rfid_tag_reads`, `retail.inventory.stockout_count`,
  `retail.inventory.on_hand_pct`
- Cold chain: `retail.fridge.temp_c`, `retail.fridge.compressor_amps`,
  `retail.fridge.door_open_count`, `retail.hvac.zone_temp_c`, `retail.fridge.energy_kwh`
- Network: `retail.network.client_count`, `retail.network.uplink_mbps`,
  `retail.network.wan_latency_ms`, `retail.network.wifi_auth_failures`,
  `retail.network.client_rssi_dbm`
- Digital: `retail.ecom.checkout_latency_ms`, `retail.ecom.cart_value_usd`,
  `retail.ecom.checkout_success_rate`, `retail.ecom.search_latency_ms`,
  `retail.ecom.add_to_cart_count`
- Filter by `retail.assetgroup` = pos / payments / inventory / fridge / network / digital
- Filter by `retail.store_id` = store-NNN, or `retail.banner`, or `retail.region`

**Logs** → Log Viewer:
- Filter: `asset.id` contains `pos-` / `pay-` / `scn-` / `frz-` / `ap-` / `ecom-`
- Look for: CHECKOUT-COMPLETE, EMV-TIMEOUT, DRAWER-JAM, PAYMENT-DECLINE, FRAUD-BLOCK,
  CHARGEBACK-RECEIVED, STOCKOUT, SHRINKAGE-EVENT, RFID-SCAN, **TEMP-EXCURSION**,
  **SPOILAGE-RISK** (food-safety!), COMPRESSOR-FAIL, WAN-LINK-DOWN, DHCP-POOL-EXHAUSTED,
  ECOM-5XX, CART-ABANDON, CHECKOUT-OUTAGE events

**Distributed Traces** → Trace search:
- Service namespace: `retail`
- Root spans: `customer_transaction` with `operation.type` ∈
  {`purchase`, `return`, `void`, `exchange`}
- Children: `scan` → `totalize` → `tender` → `authorize` → (`settle` → `receipt`)
  or (`notify_customer` on decline)
- Filter by `tender.type` (credit / debit / gift_card / ebt / cash / mobile_wallet)

**If telemetry is missing:**
- Run `scripts/logs.sh --demo retail` to tail collector logs on the VM.
- Confirm all 7 collectors show **Connected** (not just gateway).
- Verify `DT_OTLP_ENDPOINT` and `DT_API_TOKEN` are correct in `/opt/demo/.env` on the VM
  (`ssh <vm> sudo cat /opt/demo/.env`).

---

## Live Demo Change — the "wow moment"

The pipeline is already running. The live demo shows BindPlane pushing a pipeline change
fleet-wide in real time, with zero SSH and zero restarts.

### Option A — Filter out info-severity store chatter (volume reduction)

**Scenario:** a retail-IT policy suppresses chatty informational POS scan + Wi-Fi client-associate
events to reduce log ingest cost. This is the highest-impact demo for a chain operator facing
growing per-store log volume from hundreds of stores.

1. In BindPlane UI → **Configurations** → `retail-gateway`
2. Click **Edit**. On the `otlp` source (logs pipeline), click **Add Processor**.
3. Choose **Filter Severity**. Configure:
   - Action: drop logs where `severity < WARNING`
4. Click **Save** — BindPlane displays the pending config diff.
5. Click **Rollout** → choose **Progressive** (1 agent first, then all).

**Talking points while the UI updates:**
- "Watch BindPlane push the new pipeline to the gateway — no SSH, no restart at any of the 250
  stores, no field tech visit."
- "Log volume in Dynatrace drops immediately — only Warning+ events from POS, payments,
  inventory, fridge, network, and digital."
- "The filter decision is centralized at the gateway. One change, fleet-wide effect across
  every store and every asset group."
- "Critically, the `TEMP-EXCURSION` and `SPOILAGE-RISK` fridge events are ERROR / CRITICAL
  severity — they stay. Food safety alerting is preserved while we cut 60-80% of the noise."
- "To roll back: hit Revert — same diff, same Rollout mechanism, same convergence view."

Verify in Dynatrace Log Viewer: count of log events drops; only Warning and above visible.

### Option B — Add banner and cost-center attributes (attribute enrichment)

**Scenario:** a finance team needs every retail metric and log tagged with `cost_center` for
chargeback and with `retail.banner` already present (so they can roll up by banner brand). Push
the change at the gateway — no per-store POS or payment terminal touched.

1. In BindPlane UI → **Configurations** → `retail-gateway`
2. Click **Edit**. On the `otlp` source, click **Add Processor**.
3. Choose **Transform**. Configure:
   - Telemetry type: `Metrics`
   - Statements (resource context):
     ```
     set(attributes["cost_center"], "RETAIL-OPS-EAST")
     set(attributes["fiscal_year"], "FY26")
     set(attributes["demo"], "retail")
     ```
4. Add the same Transform on the `Logs` pipeline.
5. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "In ~30 seconds, new metrics and logs in Dynatrace will carry `cost_center=RETAIL-OPS-EAST`
  and `fiscal_year=FY26`."
- "No SSH to any POS, payment terminal, RFID scanner, or fridge controller. No collector
  restart. The pipeline change propagates via OpAMP."
- "Revert is one click — BindPlane shows the exact config diff both ways."

Verify in Dynatrace Metrics Explorer: filter by attribute `cost_center = RETAIL-OPS-EAST`.
