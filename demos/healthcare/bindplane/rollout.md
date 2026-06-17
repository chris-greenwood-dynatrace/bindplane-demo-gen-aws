# BindPlane Rollout — Healthcare Demo

> **API-applied pipelines.** `scripts/up.sh` calls `bindplane apply` automatically after the VM is
> up. You do NOT need to build anything in the BindPlane UI. Collectors enroll via OpAMP, BindPlane
> matches them by label, and the pipelines are pushed within ~60 seconds.
> **All telemetry in this demo is simulator-generated** — no real patients, providers, medications,
> medical devices, EHR systems, or facility BMS controllers. Identifiers (MRN, employee, NDC,
> accession, order id) are randomly generated and not associated with any actual person.

## What up.sh does automatically

1. Terraform provisions the Azure VM and writes `/opt/demo/.env` (contains `DT_OTLP_ENDPOINT`,
   `DT_API_TOKEN`, `BP_OPAMP_ENDPOINT`, `BP_SECRET_KEY`).
2. Docker Compose starts all 7 BDOT collectors + simulators on the VM. Collectors enroll to
   BindPlane Cloud over OpAMP immediately.
3. `bindplane apply -f bindplane/destinations.yaml` creates (or updates) the two Destination
   resources: `dynatrace-healthcare` and `gateway-otlp-healthcare`.
4. `bindplane apply -f bindplane/configurations.yaml` creates (or updates) the two Configuration
   resources. BindPlane immediately begins pushing the matching pipeline to each enrolled collector.
5. `bindplane rollout start healthcare-gateway` and `bindplane rollout start healthcare-edge`
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
2. Go to **Agents** and filter by `demo=healthcare`. All 7 collectors should appear
   **Connected** within ~60 seconds of the VM starting.

Expected collector to configuration assignments:

| Collector  | Configuration       | Selector match               |
|------------|---------------------|------------------------------|
| gateway    | healthcare-gateway  | role=gateway, demo=healthcare |
| bedside    | healthcare-edge     | role=edge, demo=healthcare    |
| infusion   | healthcare-edge     | role=edge, demo=healthcare    |
| imaging    | healthcare-edge     | role=edge, demo=healthcare    |
| pharmacy   | healthcare-edge     | role=edge, demo=healthcare    |
| ehr        | healthcare-edge     | role=edge, demo=healthcare    |
| facility   | healthcare-edge     | role=edge, demo=healthcare    |

3. If any collector shows **Config Pending** after 2 minutes: in the UI, open the Configuration
   and click **Rollout** to force a push.

## Verify telemetry in Dynatrace

Open `https://<DT_ENV_ID>.live.dynatrace.com`

**Metrics** → Metrics Explorer:
- Bedside: `healthcare.bedside.heart_rate_bpm`, `healthcare.bedside.spo2_pct`,
  `healthcare.bedside.bp_systolic_mmhg`, `healthcare.bedside.bp_diastolic_mmhg`,
  `healthcare.bedside.alarm_count`
- Infusion: `healthcare.infusion.rate_ml_per_hr`, `healthcare.infusion.occlusion_pressure_psi`,
  `healthcare.infusion.battery_pct`, `healthcare.infusion.dose_error_count`,
  `healthcare.infusion.drug_library_version`
- Imaging: `healthcare.imaging.studies_per_hour`, `healthcare.imaging.acquisition_latency_ms`,
  `healthcare.imaging.magnet_helium_pct` (MRI only), `healthcare.imaging.tube_temp_c`,
  `healthcare.imaging.queue_depth`
- Pharmacy / cold chain: `healthcare.pharmacy.fridge_temp_c`,
  `healthcare.pharmacy.compressor_amps`, `healthcare.pharmacy.access_count`,
  `healthcare.pharmacy.diversion_alert_count`, `healthcare.pharmacy.stock_pct`
- EHR: `healthcare.ehr.chart_open_latency_ms`, `healthcare.ehr.login_count`,
  `healthcare.ehr.session_count`, `healthcare.ehr.search_latency_ms`,
  `healthcare.ehr.unsigned_orders_count`
- Facility / BMS: `healthcare.or.positive_pressure_inwc` (life-safety),
  `healthcare.or.air_changes_per_hour`, `healthcare.iso.negative_pressure_inwc`
  (infection control), `healthcare.hvac.zone_temp_c`, `healthcare.nurse_call.active_count`
- Filter by `healthcare.assetgroup` = bedside / infusion / imaging / pharmacy / ehr / facility
- Filter by `healthcare.facility_id` = facility-NNN, `healthcare.unit` (ICU / ED / OR / ONC /
  MED-SURG / PEDS / L-D / RAD / PHARM / TELE), or `healthcare.health_system`

**Logs** → Log Viewer:
- Filter: `asset.id` contains `bed-` / `pump-` / `mod-` / `adc-` / `wks-` / `bms-`
- Look for: VITALS-NORMAL, ARRHYTHMIA-DETECTED, APNEA-ALARM, **CARDIAC-ARREST-ALARM** (code blue),
  OCCLUSION-ALARM, AIR-IN-LINE-ALARM, **BCMA-MISMATCH-PUMP-ABORT** (patient-safety event),
  DICOM-STUDY-TIMEOUT, MODALITY-OFFLINE, **MAGNET-QUENCH-WARNING** (multi-$M MRI event),
  ADC-DISPENSE, **VACCINE-COLD-CHAIN-EXCURSION**, DEA-DISCREPANCY,
  **VACCINE-DOSES-AT-RISK** (life-safety + dollar-loss),
  **CONTROLLED-SUBSTANCE-DIVERSION-ALERT** (DEA-reportable),
  CHART-OPEN-SLOW, EHR-5XX, INTERFACE-ENGINE-LAG, **EHR-OUTAGE-DOWNTIME-PROCEDURE**,
  OR-POSITIVE-PRESSURE-LOST (infection control),
  **ISO-NEGATIVE-PRESSURE-LOST** (airborne pathogen exposure — critical)

**Distributed Traces** → Trace search:
- Service namespace: `healthcare`
- Root spans: `medication_administration` with `operation.type` ∈
  {`scheduled_dose`, `prn_dose`, `stat_dose`, `continuous_infusion`}
- Children: `order_received` → `verify_5_rights` → `pull_from_adc` → `barcode_scan_patient`
  → `barcode_scan_drug` → `pump_program` → `pump_start` → `infusion_complete`
  (or `alarm_occlusion` → `notify_clinician` on fail; root span tagged with `abort.stage`)
- Filter by `medication.class` (opioid / antibiotic / chemotherapy / electrolyte /
  anticoagulant / vasopressor / insulin / antiemetic / analgesic)

**If telemetry is missing:**
- Run `scripts/logs.sh --demo healthcare` to tail collector logs on the VM.
- Confirm all 7 collectors show **Connected** (not just gateway).
- Verify `DT_OTLP_ENDPOINT` and `DT_API_TOKEN` are correct in `/opt/demo/.env` on the VM
  (`ssh <vm> sudo cat /opt/demo/.env`).

---

## Live Demo Change — the "wow moment"

The pipeline is already running. The live demo shows BindPlane pushing a pipeline change
fleet-wide in real time, with zero SSH and zero restarts.

### Option A — Filter out info-severity clinical-IT chatter (volume reduction)

**Scenario:** a clinical-IT policy suppresses high-volume informational chatter (VITALS-NORMAL,
CHART-OPEN, ADC-DISPENSE, OR-PRESSURE-NORMAL) to reduce log ingest cost. This is the highest-impact
demo for an IDN managing log volume across dozens of facilities.

1. In BindPlane UI → **Configurations** → `healthcare-gateway`
2. Click **Edit**. On the `otlp` source (logs pipeline), click **Add Processor**.
3. Choose **Filter Severity**. Configure:
   - Action: drop logs where `severity < WARNING`
4. Click **Save** — BindPlane displays the pending config diff.
5. Click **Rollout** → choose **Progressive** (1 agent first, then all).

**Talking points while the UI updates:**
- "Watch BindPlane push the new pipeline to the gateway — no SSH, no restart at any of the 25
  facilities, no biomed engineer visit to any clinical floor."
- "Log volume in Dynatrace drops immediately — only Warning+ events from bedside monitors,
  pumps, modalities, ADCs, EHR workstations, and the BMS."
- "Critically, every patient-safety and life-safety event is ERROR or CRITICAL severity —
  they stay. **CARDIAC-ARREST-ALARM**, **BCMA-MISMATCH-PUMP-ABORT**,
  **VACCINE-COLD-CHAIN-EXCURSION**, **VACCINE-DOSES-AT-RISK**,
  **CONTROLLED-SUBSTANCE-DIVERSION-ALERT**, **MAGNET-QUENCH-WARNING**,
  **OR-POSITIVE-PRESSURE-LOST**, and **ISO-NEGATIVE-PRESSURE-LOST** all pass through unchanged.
  Patient-safety, infection-control, and DEA-regulatory alerting is preserved while we cut
  60–80% of the noise."
- "To roll back: hit Revert — same diff, same Rollout mechanism, same convergence view."

Verify in Dynatrace Log Viewer: count of log events drops; only Warning and above visible.

### Option B — Add cost-center and compliance attributes (attribute enrichment)

**Scenario:** a finance team needs every healthcare metric and log tagged with `cost_center`
for service-line chargeback, plus a `compliance.scope` attribute so a HIPAA/HITECH audit can
filter the audit trail. Push the change at the gateway — no per-clinic biomed device touched.

1. In BindPlane UI → **Configurations** → `healthcare-gateway`
2. Click **Edit**. On the `otlp` source, click **Add Processor**.
3. Choose **Transform**. Configure:
   - Telemetry type: `Metrics`
   - Statements (resource context):
     ```
     set(attributes["cost_center"], "CLINICAL-OPS-EAST")
     set(attributes["fiscal_year"], "FY26")
     set(attributes["compliance.scope"], "HIPAA-HITECH")
     set(attributes["demo"], "healthcare")
     ```
4. Add the same Transform on the `Logs` pipeline.
5. Click **Save** → **Rollout** → **Progressive**.

**Talking points:**
- "In ~30 seconds, new metrics and logs in Dynatrace will carry `cost_center=CLINICAL-OPS-EAST`
  and `compliance.scope=HIPAA-HITECH`."
- "No SSH to any patient monitor, infusion pump, imaging modality, ADC, EHR workstation, or
  BMS controller. No collector restart. The pipeline change propagates via OpAMP."
- "For an auditor, this means the entire observability trail across biomed + clinical IT + BMS
  is filterable by a single attribute. For finance, every signal rolls up to a cost center."
- "Revert is one click — BindPlane shows the exact config diff both ways."

Verify in Dynatrace Metrics Explorer: filter by attribute `cost_center = CLINICAL-OPS-EAST`.
