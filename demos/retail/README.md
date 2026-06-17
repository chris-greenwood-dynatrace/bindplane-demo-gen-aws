# Retail Demo — Store Edge Fleet (POS · Payments · Inventory · Cold Chain · Network · Digital)

This demo shows BindPlane managing seven OpenTelemetry collectors monitoring a simulated retail chain's per-store edge: six edge collectors (pos, payments, inventory, fridge, network, digital) each receive metrics, logs, and traces from a unified `device-sim` container running in their asset group, which simulates DEVICE_COUNT assets per group (POS terminals/NCR, payment terminals + gateways/Ingenico, RFID + handheld scanners/Zebra, refrigeration + HVAC/Emerson — the cold-chain food-safety story, in-store Wi-Fi + WAN/Meraki, and e-commerce + mobile-app digital experience/Shopify-style stack) — every asset emits all three signals over OTLP gRPC with identical resource attributes (`service.name`, `host.name`, `asset.id`, `asset.type`, `asset.vendor`, `retail.assetgroup`, `retail.region`, `retail.store_id`, `retail.banner`, plus `retail.fridge.subtype` for refrigeration and `retail.digital.channel` for digital) so Dynatrace correlates metrics, logs, and traces into a single entity per asset without any join configuration; metrics include POS health (txn_per_min, basket_usd, avg_scan_ms, printer_online, void_count), payments (auth_latency_ms, approval_rate, decline_rate, chargeback_count, amount_usd), inventory operations (scan_per_min, scan_failure_rate, rfid_tag_reads, stockout_count, on_hand_pct), cold chain + HVAC (fridge temp_c, compressor_amps, door_open_count, hvac zone_temp_c, fridge energy_kwh), in-store network (client_count, uplink_mbps, wan_latency_ms, wifi_auth_failures, client_rssi_dbm), and digital experience (ecom checkout_latency_ms, cart_value_usd, checkout_success_rate, search_latency_ms, add_to_cart_count); logs are retail-style structured records covering CHECKOUT-COMPLETE / DRAWER-JAM / EMV-TIMEOUT / PRINTER-OFFLINE at POS, AUTH-APPROVED / PAYMENT-DECLINE / FRAUD-BLOCK / CHARGEBACK-RECEIVED / GATEWAY-UNREACHABLE at payments, RFID-SCAN / STOCKOUT / SHRINKAGE-EVENT / AUDIT-DISCREPANCY in inventory, TEMP-EXCURSION / SPOILAGE-RISK / COMPRESSOR-FAIL / DOOR-LEFT-OPEN / HVAC-SETPOINT-DRIFT in cold chain (CRITICAL events are food-safety / regulatory), WIFI-AUTH-FAIL / WAN-LINK-DOWN / DHCP-POOL-EXHAUSTED / UPLINK-DEGRADED in network, and ADD-TO-CART / CART-ABANDON / ECOM-5XX / SEARCH-SLOW / CHECKOUT-OUTAGE in digital at varied severities; traces model multi-span `customer_transaction` workflows (`scan` → `totalize` → `tender` → `authorize` → `settle` → `receipt`, or `notify_customer` on decline) covering the canonical retail business transaction with `operation.type` ∈ {`purchase`, `return`, `void`, `exchange`} and `tender.type` ∈ {`credit`, `debit`, `gift_card`, `ebt`, `cash`, `mobile_wallet`}; BindPlane pushes the two Configuration pipelines (one gateway, one edge) to the matching agents via OpAMP within ~60 seconds and the demo's live "wow moment" runbook in `bindplane/rollout.md` shows how to add a Filter Severity processor at the gateway — preserving the critical TEMP-EXCURSION / SPOILAGE-RISK food-safety alerts while cutting 60–80% of informational store-chatter — or push a `cost_center=RETAIL-OPS-EAST` resource attribute fleet-wide, all without SSH and without restarting a single in-store device.

## Files

- `manifest.yaml` — single source of truth (collectors, signals, caps, image)
- `docker-compose.yaml` — 7 BDOT collectors + 6 device-sim services on the `store` network
- `.env.demo` — non-secret tuning knobs (DEVICE_COUNT, FAILURE_RATE, INTENSITY)
- `collectors/*.env` — per-collector `OPAMP_LABELS` (subset matched to BindPlane Configurations)
- `simulators/device-sim/` — unified Python OTel simulator (one image, six service instances)
- `bindplane/destinations.yaml` — managed `dynatrace_otlp` destination + edge→gateway OTLP gRPC
- `bindplane/configurations.yaml` — `retail-gateway` and `retail-edge` Configurations
- `bindplane/fleets.yaml` — Fleets pairing each Configuration with its role-keyed selector
- `bindplane/rollout.md` — verify checklist + live "wow moment" demo runbook

## Run

```bash
scripts/up.sh --demo retail
scripts/logs.sh --demo retail        # tail collector + simulator logs on the VM
scripts/down.sh --demo retail        # destroy infra (atomic)
```

## Talking points

- "BindPlane is managing seven OpenTelemetry collectors covering the per-store edge for a retail
  chain: POS, payments, inventory, refrigeration, in-store network, and e-commerce — under the
  free-plan cap of ten."
- "Every store-edge asset emits metrics, logs, and traces with identical OTel resource
  attributes — including a `retail.store_id` and `retail.banner` — so Dynatrace shows one entity
  per asset and correlates incidents across the OT (refrigeration) ↔ IT (POS, network) ↔
  business (transactions, e-com cart) layers."
- "The canonical retail business transaction is right here in the trace view:
  `customer_transaction` → `scan → totalize → tender → authorize → settle → receipt`. When the
  EMS authorizer declines, the span flips to ERROR and the path becomes
  `… → authorize (FAIL) → notify_customer`."
- "When a fridge fault is injected, you see `TEMP-EXCURSION` in logs at ERROR, the
  `retail.fridge.temp_c` metric spike, and — if it persists — a CRITICAL `SPOILAGE-RISK` log with
  an estimated dollar-loss tag. That's the food-safety pitch for grocery and QSR."
- "I can change the pipeline shape — filter info logs, add a `cost_center=RETAIL-OPS-EAST`
  attribute, route a store group somewhere else — and BindPlane pushes it fleet-wide
  over OpAMP in about thirty seconds, with no SSH and no field tech visit to any of the stores."

## Business variants

| Variant       | Pitch / overrides                                                                                |
|---------------|--------------------------------------------------------------------------------------------------|
| `bigbox`      | Target/Walmart/Costco — heavy POS + payments + network volume; cost_center=BIGBOX-OPS            |
| `grocery`     | Kroger/Albertsons — cold-chain is critical; emphasize `fridge` + `inventory`; SPOILAGE-RISK pitch |
| `specialty`   | Sephora/Best Buy — high AOV; emphasize `digital` + `payments`; chargeback + fraud-block stories  |
| `qsr`         | McDonald's/Chipotle — POS + kitchen + delivery; emphasize `pos` + `fridge`; speed-of-service     |
| `convenience` | 7-Eleven/Sheetz — POS + fuel + cold-case; add a fuel-pump dimension via DEVICE_COUNT on `pos`    |

## Architecture notes

- One Azure VM (`Standard_B2ms`) runs the entire compose stack. The 6 edge collectors model
  *per-asset-group* aggregation across many physical stores (encoded via `retail.store_id` —
  each simulated asset is randomly assigned to one of 250 stores). This stays under the
  10-collector free-plan cap while still telling the multi-store fleet story.
- Only the gateway collector holds the Dynatrace API token (in the BindPlane-managed
  `dynatrace_otlp` destination). Edge collectors forward OTLP to the gateway via the
  internal `otlp_grpc` destination.
- The managed `dynatrace_otlp` destination handles delta temporality conversion internally —
  no `cumulativetodelta` processor is required.
- The `device-sim` container is one image with a `PROFILES` switch (`pos`, `payments`,
  `inventory`, `fridge`, `network`, `digital`) — adding a new asset group is a one-block edit
  to `sim.py` plus a new compose service.
