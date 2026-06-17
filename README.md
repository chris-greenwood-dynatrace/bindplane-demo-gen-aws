# bindplane-demo

Repeatable Dynatrace SE demos showing **BindPlane** (now a Dynatrace product) managing **fleets of
OpenTelemetry collectors** (BDOT — BindPlane Distro for OpenTelemetry). Each demo spins up an
ephemeral Azure VM running the fleet + telemetry simulators; **logs, metrics, and traces** flow
through BindPlane to Dynatrace. One demo runs at a time, selected at spin-up.

| Demo | Collectors | Story |
|---|---|---|
| `manufacturing` | 6 | Factory machine fleet — production lines, packaging, plant utilities. Each device emits metrics+logs+traces (per-device unified signals). Reusable across automotive / food&bev / pharma / electronics / metals. |
| `networking` | 5 | NOC device fleet — core/edge routers & switches, firewalls, load balancers. Each device emits metrics+logs+traces via OTLP. Reusable across enterprise / ISP / campus / datacenter / retail-WAN. |
| `energy` | 7 | Grid asset fleet — HV substations (SEL relays), distribution transformers (ABB), feeders + reclosers (S&C), AMI smart-meter concentrators (Itron), generation (solar/wind/battery/gas peaker, GE), and SCADA RTUs (Hitachi). Every asset emits metrics+logs+traces with one entity per asset. Reusable across IOU / co-op / municipal / T&D-only / ISO-RTO. |
| `retail` | 7 | Per-store edge fleet — POS terminals (NCR), payment terminals + gateway (Ingenico), RFID + handheld scanners (Zebra), refrigeration + HVAC (Emerson) for the cold-chain / food-safety story, in-store Wi-Fi + WAN (Meraki), and e-commerce + mobile-app digital experience (Shopify-style). Every asset carries a `retail.store_id` so 250 simulated stores fold into 6 edge groups. Reusable across big-box / grocery / specialty / QSR / convenience. |
| `healthcare` | 7 | Hospital edge fleet — bedside patient monitors (Philips), smart infusion pumps + BCMA (Baxter), imaging modalities MRI/CT/X-ray (Siemens), pharmacy ADCs + vaccine cold chain + DEA controlled-substance safes (Omnicell), EHR workstations + clinical kiosks (Epic-style), and facility BMS for OR positive-pressure / isolation negative-pressure / nurse call (Johnson Controls). Every asset carries `healthcare.facility_id` + `healthcare.unit` so 25 simulated facilities across 10 hospital units fold into 6 edge groups. Reusable across academic medical centers / community hospitals / IDNs / ASCs / long-term care. |

> All device/machine telemetry is **simulator-generated** (industrial + network protocols have no
> first-class BindPlane source). The demo authentically shows BindPlane *managing collectors and
> pipelines* and Dynatrace *ingesting all three signals* — not real PLCs/routers.

---

## How it works

```
scripts/up.sh --demo <name>
  → terraform apply -var demo=<name>      # 1 Azure Linux VM, single resource group
  → cloud-init installs Docker; up.sh copies the demo + runs docker compose
  → BDOT collectors enroll to BindPlane Cloud over OpAMP (endpoint + secret + per-collector labels)
  → simulators feed edge collectors; each device emits metrics+logs+traces → one correlated entity
  → scripts/bp-apply.sh (operator's machine, requires bindplane CLI):
       bindplane apply -f destinations.yaml
       bindplane apply -f configurations.yaml
       bindplane rollout start <configuration-name>   # for each Configuration
  → BindPlane pushes pipelines to matching collectors (~60s OpAMP heartbeat)
  → gateway collector exports via dynatrace_otlp destination → Dynatrace
scripts/down.sh  → drains collectors (frees the cap) → terraform destroy (atomic)
scripts/down.sh --purge-bindplane  → also removes BindPlane Agents, Fleets, Configurations, and Destinations
```

Pipelines are **applied automatically** by `up.sh` via the **`bindplane` CLI** (local prerequisite
on the operator's machine — not the VM). Only the **gateway** collector holds the Dynatrace token
(in the BindPlane-managed `dynatrace_otlp` destination, which handles delta temporality internally);
edge collectors forward OTLP to it via an internal `otlp_grpc` destination.
See [CLAUDE.md](CLAUDE.md) for the full architecture and the **demo contract** (how to add a demo).

---

## Prerequisites

1. **Tooling** (local): Terraform ≥ 1.5, Azure CLI (`az login`), Docker, `yq`, `rsync`, an SSH
   key, and the **`bindplane` CLI** (v1.98.3+):
   ```bash
   brew tap observiq/bindplane && brew install bindplane   # macOS
   # all platforms: https://docs.bindplane.observiq.com/docs/install-cli
   ```
   The `bindplane` CLI applies pipelines from your machine directly to BindPlane Cloud — it is NOT
   installed on the demo VM.
2. **BindPlane Cloud** free account + 1 project. From the UI copy:
   - *Agents → Install Agent*: **OpAMP endpoint** (`wss://app.bindplane.com/v1/opamp`) + **secret key**
   - *Settings → API Keys*: generate a **BP_API_KEY** — the free plan has full REST API access.
   > Limits: **1 project, 10 collectors, 10 GB/day**. Both demos stay within these.
3. **Dynatrace**: your environment id (`abc12345`) and an **OTLP ingest token**.
   > ⚠️ OTLP ingest (`/api/v2/otlp`) expects an **access token** (`dt0c01.*`) with scopes
   > `metrics.ingest`, `logs.ingest`, `openTelemetryTrace.ingest`. A **platform token** (`dt0s16.*`)
   > may not work for classic OTLP ingest — verify, or mint an access token with those three scopes.
4. **Secrets**: `cp .env.example .env` and fill in `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`, `BP_API_KEY`,
   `DT_ENV_ID`, `DT_API_TOKEN`, plus Azure knobs. `.env` is gitignored.

---

## Quickstart

```bash
cp .env.example .env          # fill in BindPlane + Dynatrace + Azure values
./scripts/demos.sh list       # see available demos
./scripts/up.sh --demo manufacturing     # validates, provisions VM, boots fleet, applies pipelines
# ... give the demo ...
./scripts/down.sh             # drains collectors, destroys all Azure resources
```

`up.sh` runs `scripts/validate.sh <demo>` first and aborts if any rule fails (collector cap,
3-signal completeness, `dynatrace_otlp` destination with env-var credentials, label↔selector
agreement, pinned image, no committed secrets). Then after the fleet is up, it automatically
applies the BindPlane pipeline Configurations and Destinations via `scripts/bp-apply.sh`.

### 3. Optional: Live Rollout demo in the BindPlane UI

Pipelines are applied automatically — no UI build step required. The runbooks are now optional
**live-demo highlights** showing BindPlane rolling a pipeline change across the fleet in real time:

- Manufacturing: [demos/manufacturing/bindplane/rollout.md](demos/manufacturing/bindplane/rollout.md)
- Networking: [demos/networking/bindplane/rollout.md](demos/networking/bindplane/rollout.md)
- Energy: [demos/energy/bindplane/rollout.md](demos/energy/bindplane/rollout.md)
- Retail: [demos/retail/bindplane/rollout.md](demos/retail/bindplane/rollout.md)
- Healthcare: [demos/healthcare/bindplane/rollout.md](demos/healthcare/bindplane/rollout.md)

The live Rollout step (edit a processor in the UI → roll out to a labeled subset → watch agents
converge → show data change in Dynatrace) is the highest-impact demo moment.

### 4. Verify in Dynatrace

Confirm all three signals arrived (DQL or the dt-obs-* tooling):
- **Metrics** — machine temp/vibration/OEE (mfg) · interface octets/CPU/sessions (net) · substation voltage_kv, transformer oil_temp_c, feeder load_amps, gen output_mw, battery soc_pct, scada poll_latency_ms (energy) · pos.txn_per_min, payment.auth_latency_ms, fridge.temp_c, network.client_count, ecom.checkout_latency_ms, inventory.stockout_count (retail) · bedside.heart_rate_bpm, bedside.spo2_pct, infusion.occlusion_pressure_psi, infusion.dose_error_count, imaging.magnet_helium_pct, pharmacy.fridge_temp_c, pharmacy.diversion_alert_count, ehr.chart_open_latency_ms, or.positive_pressure_inwc, iso.negative_pressure_inwc (healthcare).
- **Logs** — machine alarms (mfg) · device syslog (net) · SEL relay trips, oil-temp warnings, recloser ops, AMI comm failures, RTU heartbeat losses (energy) · EMV-TIMEOUT, PAYMENT-DECLINE, FRAUD-BLOCK, STOCKOUT, TEMP-EXCURSION / SPOILAGE-RISK (food safety), WAN-LINK-DOWN, ECOM-5XX (retail) · CARDIAC-ARREST-ALARM, BCMA-MISMATCH-PUMP-ABORT (patient safety), MAGNET-QUENCH-WARNING, VACCINE-COLD-CHAIN-EXCURSION / VACCINE-DOSES-AT-RISK (life-safety), CONTROLLED-SUBSTANCE-DIVERSION-ALERT (DEA), EHR-OUTAGE-DOWNTIME-PROCEDURE, OR-POSITIVE-PRESSURE-LOST, ISO-NEGATIVE-PRESSURE-LOST (infection control) (healthcare).
- **Traces** — MES job execution (mfg) · network provisioning (net) · grid_operation (fault_isolation / load_shed / restoration / dispatch) for energy · customer_transaction (scan → totalize → tender → authorize → settle → receipt, or notify_customer on decline) for retail · medication_administration (order_received → verify_5_rights → pull_from_adc → barcode_scan_patient → barcode_scan_drug → pump_program → pump_start → infusion_complete, or alarm_occlusion → notify_clinician on fail; failure modes for BCMA-patient mismatch, BCMA-drug mismatch, and occlusion) for healthcare.

---

## Teardown

```bash
./scripts/down.sh --demo <name>
```

Drains the collectors over SSH first (so they disconnect and **free the 10-collector cap
immediately**), then `terraform destroy` removes the resource group. The BindPlane Configurations
remain in your project by design — they're reused on the next spin-up.

Add `--purge-bindplane` to also delete the demo's Agents, Fleets, Configurations, and Destinations
from BindPlane (in dependency order — agents must be removed before their parent fleets):

```bash
./scripts/down.sh --demo <name> --purge-bindplane
```

---

## Switching demos

Only one demo runs at a time (10-collector cap). `down.sh` the current demo, then `up.sh --demo`
the other. Both demos' pipelines coexist in the single BindPlane project, matched by the
`demo=<name>` label, so only the running demo's collectors are active.

---

## Adding a demo

The framework is convention-driven — a new demo is **one folder, zero Terraform change**:

```bash
# via the demo-scaffold skill, or:
cp -r demos/_template demos/<name>
# fill in manifest.yaml (≤10 collectors, labels, signal map), simulators/, bindplane/, collectors/
./scripts/validate.sh <name>     # must pass before deploy
```

`scripts/demos.sh list` auto-discovers it. See [CLAUDE.md](CLAUDE.md) for the demo contract and the
`.claude/` agents (`terraform-azure`, `bindplane-pipeline`, `otel-simulator`) and skills
(`demo-scaffold`, `bindplane-validate`) that help build demos correctly.

---

## Repo layout

```
terraform/        demo-agnostic Azure root (1 VM, 1 resource group) + cloud-init
scripts/          up / down / select / ssh / logs / validate / demos(registry) + lib/common.sh
demos/_template/  scaffold source for new demos
demos/<name>/     manifest.yaml · docker-compose.yaml · collectors/ · simulators/ · bindplane/
.claude/          project subagents + skills
CLAUDE.md         conventions + demo contract (read before adding a demo)
```

## Cost & safety

Single `Standard_B2s`/`B2ms` VM, no persistent disks, one resource group. Always `down.sh` after a
session; consider an Azure auto-shutdown schedule as a backstop. Secrets live only in gitignored
`.env` / `secrets.auto.tfvars` and the VM's root-owned `/opt/demo/.env`.

## Multi-operator isolation

Every Azure resource name embeds a per-operator `owner` tag — pattern
`<prefix>-<owner>-<demo>` (e.g. `rg-bpdemo-jdoe-energy`, `vm-bpdemo-jdoe-energy`). `up.sh` derives
`owner` automatically from `OWNER_TAG` in `.env`, falling back to `whoami` (sanitised to ≤12
lowercase alphanumerics). Two SEs running the same demo in the same Azure subscription will get
distinct resource groups and never step on each other. Resources are also tagged `owner=<value>`
for portal filtering and cost attribution. Override with `OWNER_TAG=jdoe` in `.env` for CI or
shared service accounts.
