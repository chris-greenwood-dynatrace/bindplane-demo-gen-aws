# Healthcare Demo — Hospital Edge Fleet (Bedside · Infusion · Imaging · Pharmacy · EHR · Facility)

This demo shows BindPlane managing seven OpenTelemetry collectors monitoring a simulated hospital's clinical edge: six edge collectors (bedside, infusion, imaging, pharmacy, ehr, facility) each receive metrics, logs, and traces from a unified `device-sim` container running in their asset group, which simulates DEVICE_COUNT assets per group (bedside patient monitors/Philips IntelliVue, smart infusion pumps + BCMA/Baxter Spectrum, imaging modalities — MRI 3T / MRI 1.5T / CT 64-slice / X-ray DR/Siemens, pharmacy automated dispensing cabinets + vaccine/biologics cold chain + DEA controlled-substance safes/Omnicell — the cold-chain life-safety + regulatory story, EHR workstations + clinical kiosks + nurse mobile + provider laptop channels/Epic-style, and facility BMS for OR positive-pressure / isolation negative-pressure / HVAC / nurse call/Johnson Controls) — every asset emits all three signals over OTLP gRPC with identical resource attributes (`service.name`, `host.name`, `asset.id`, `asset.type`, `asset.vendor`, `healthcare.assetgroup`, `healthcare.region`, `healthcare.facility_id`, `healthcare.unit`, `healthcare.health_system`, plus `healthcare.pharmacy.subtype` for refrigeration / vaccine ultra-cold / blood bank / DEA safe, `healthcare.ehr.channel` for ehr, and `healthcare.imaging.modality` + `healthcare.imaging.magnet_field_t` for imaging) so Dynatrace correlates metrics, logs, and traces into a single entity per asset without any join configuration; metrics include bedside vitals (heart_rate_bpm, spo2_pct, bp_systolic/diastolic_mmhg, alarm_count), infusion-pump operation (rate_ml_per_hr, occlusion_pressure_psi, battery_pct, dose_error_count, drug_library_version), imaging throughput + health (studies_per_hour, acquisition_latency_ms, magnet_helium_pct for MRI, tube_temp_c for CT, queue_depth), pharmacy cold-chain + access (fridge_temp_c, compressor_amps, access_count, diversion_alert_count, stock_pct), EHR clinical-IT (chart_open_latency_ms, login_count, session_count, search_latency_ms, unsigned_orders_count), and facility BMS (OR positive_pressure_inwc + air_changes_per_hour, isolation negative_pressure_inwc, hvac zone_temp_c, nurse_call active_count); logs are clinical-style structured records covering VITALS-NORMAL / LEAD-OFF / ARRHYTHMIA-DETECTED / APNEA-ALARM / CARDIAC-ARREST-ALARM at bedside, DOSE-START / OCCLUSION-ALARM / AIR-IN-LINE-ALARM / DRUG-LIBRARY-OUTDATED / BCMA-MISMATCH-PUMP-ABORT at infusion (CRITICAL events are patient-safety), STUDY-COMPLETE / PACS-QUEUE-HIGH / DICOM-STUDY-TIMEOUT / MODALITY-OFFLINE / MAGNET-QUENCH-WARNING at imaging (multi-$M event), ADC-DISPENSE / VACCINE-COLD-CHAIN-EXCURSION / DEA-DISCREPANCY / VACCINE-DOSES-AT-RISK / CONTROLLED-SUBSTANCE-DIVERSION-ALERT at pharmacy (CRITICAL are life-safety + DEA-regulatory), CHART-OPEN / CHART-OPEN-SLOW / EHR-5XX / INTERFACE-ENGINE-LAG / EHR-OUTAGE-DOWNTIME-PROCEDURE at EHR (clinical operations switch to paper — major business event), and OR-PRESSURE-NORMAL / OR-POSITIVE-PRESSURE-LOST / ISO-NEGATIVE-PRESSURE-LOST (airborne pathogen exposure — CRITICAL infection-control) at facility BMS; traces model multi-span `medication_administration` workflows (`order_received` → `verify_5_rights` → `pull_from_adc` → `barcode_scan_patient` → `barcode_scan_drug` → `pump_program` → `pump_start` → `infusion_complete`, or `alarm_occlusion` → `notify_clinician` on fail) covering the canonical clinical business transaction with `operation.type` ∈ {`scheduled_dose`, `prn_dose`, `stat_dose`, `continuous_infusion`} and `medication.class` ∈ {`opioid`, `antibiotic`, `chemotherapy`, `electrolyte`, `anticoagulant`, `vasopressor`, `insulin`, `antiemetic`, `analgesic`} and failure modes for BCMA-patient mismatch, BCMA-drug mismatch, and downstream occlusion; BindPlane pushes the two Configuration pipelines (one gateway, one edge) to the matching agents via OpAMP within ~60 seconds and the demo's live "wow moment" runbook in `bindplane/rollout.md` shows how to add a Filter Severity processor at the gateway — preserving every CRITICAL / ERROR patient-safety, infection-control, and DEA-regulatory alert (CARDIAC-ARREST-ALARM, BCMA-MISMATCH-PUMP-ABORT, VACCINE-COLD-CHAIN-EXCURSION, CONTROLLED-SUBSTANCE-DIVERSION-ALERT, MAGNET-QUENCH-WARNING, OR-POSITIVE-PRESSURE-LOST, ISO-NEGATIVE-PRESSURE-LOST) while cutting 60–80% of informational clinical-IT chatter — or push a `cost_center=CLINICAL-OPS-EAST` + `compliance.scope=HIPAA-HITECH` resource attribute fleet-wide, all without SSH and without restarting a single bedside monitor, infusion pump, imaging modality, ADC, EHR workstation, or BMS controller.

## Files

- `manifest.yaml` — single source of truth (collectors, signals, caps, image)
- `docker-compose.yaml` — 7 BDOT collectors + 6 device-sim services on the `hospital` network
- `.env.demo` — non-secret tuning knobs (DEVICE_COUNT, FAILURE_RATE, INTENSITY)
- `collectors/*.env` — per-collector `OPAMP_LABELS` (subset matched to BindPlane Configurations)
- `simulators/device-sim/` — unified Python OTel simulator (one image, six service instances)
- `bindplane/destinations.yaml` — managed `dynatrace_otlp` destination + edge→gateway OTLP gRPC
- `bindplane/configurations.yaml` — `healthcare-gateway` and `healthcare-edge` Configurations
- `bindplane/fleets.yaml` — Fleets pairing each Configuration with its role-keyed selector
- `bindplane/rollout.md` — verify checklist + live "wow moment" demo runbook

## Run

```bash
scripts/up.sh --demo healthcare
scripts/logs.sh --demo healthcare        # tail collector + simulator logs on the VM
scripts/down.sh --demo healthcare        # destroy infra (atomic)
```

## Talking points

- "BindPlane is managing seven OpenTelemetry collectors covering the clinical edge for a hospital
  / health system: bedside monitoring, smart infusion pumps with BCMA, imaging modalities,
  pharmacy ADCs + vaccine cold chain, EHR workstations, and facility BMS for operating-room
  positive pressure and isolation-room negative pressure — under the free-plan cap of ten."
- "Every clinical-edge asset emits metrics, logs, and traces with identical OTel resource
  attributes — including `healthcare.facility_id`, `healthcare.unit`, and
  `healthcare.health_system` — so Dynatrace shows one entity per asset and correlates incidents
  across the biomed (OT) ↔ clinical IT (EHR / PACS / pump) ↔ business (medication administration,
  imaging throughput) layers."
- "The canonical clinical business transaction is right here in the trace view:
  `medication_administration` → `order_received → verify_5_rights → pull_from_adc →
  barcode_scan_patient → barcode_scan_drug → pump_program → pump_start → infusion_complete`.
  When BCMA fails, the span flips to ERROR and the path becomes `… → barcode_scan_patient (FAIL)
  → notify_clinician`. When the pump occludes, it's `… → pump_start → alarm_occlusion →
  notify_clinician`. Every failure mode points to a specific clinical workflow break."
- "When a vaccine-fridge fault is injected, you see `VACCINE-COLD-CHAIN-EXCURSION` in logs at
  ERROR, the `healthcare.pharmacy.fridge_temp_c` metric spike, and — if it persists — a CRITICAL
  `VACCINE-DOSES-AT-RISK` log with `doses_at_risk` and estimated dollar-loss tags. That's the
  cold-chain pitch for any health system carrying mRNA biologics, monoclonal antibodies, or
  routine vaccine inventories."
- "When the OR drops positive pressure, you get `OR-POSITIVE-PRESSURE-LOST` at ERROR — infection
  control gets notified, the case may be cancelled. When the isolation room goes neutral or
  positive while housing a TB / SARS-CoV-2 / measles / C.auris patient, that's a CRITICAL
  `ISO-NEGATIVE-PRESSURE-LOST` — airborne pathogen exposure. Hard-stop infection-control event."
- "I can change the pipeline shape — filter info chatter, add a `cost_center=CLINICAL-OPS-EAST`
  attribute, route a facility group somewhere else — and BindPlane pushes it fleet-wide
  over OpAMP in about thirty seconds, with no SSH and no biomed technician visit to any
  clinical floor."
- "All telemetry is simulator-generated. MRN, employee, NDC, order id, and accession are
  random. The demo is safe to run anywhere; nothing about it implies real PHI."

## Business variants

| Variant                     | Pitch / overrides                                                                                |
|-----------------------------|--------------------------------------------------------------------------------------------------|
| `academic_medical_center`   | Cleveland Clinic / Mass General — heavy imaging + research + complex meds; cost_center=AMC-RAD-ONC |
| `community_hospital`        | Smaller suburban facility — emphasize EHR uptime + ED throughput; reduce imaging modality count    |
| `idn_multi_hospital`        | HCA / CommonSpirit / Providence — multi-facility (25+ in `healthcare.facility_id`), IDN chargeback story |
| `ambulatory_surgery_center` | Outpatient — emphasize OR positive pressure + scheduling + sterile processing; reduce bedside count |
| `long_term_care`            | SNF / hospice — emphasize medication administration + falls + nurse call; reduce imaging count    |

## Architecture notes

- One Azure VM (`Standard_B2ms`) runs the entire compose stack. The 6 edge collectors model
  *per-asset-group* aggregation across many physical facilities (encoded via
  `healthcare.facility_id` — each simulated asset is randomly assigned to one of 25 facilities,
  and to one of 10 hospital units: ICU / ED / OR / ONC / MED-SURG / PEDS / L-D / RAD / PHARM /
  TELE). This stays under the 10-collector free-plan cap while still telling the multi-facility
  IDN story.
- Only the gateway collector holds the Dynatrace API token (in the BindPlane-managed
  `dynatrace_otlp` destination). Edge collectors forward OTLP to the gateway via the
  internal `otlp_grpc` destination.
- The managed `dynatrace_otlp` destination handles delta temporality conversion internally —
  no `cumulativetodelta` processor is required.
- The `device-sim` container is one image with a `PROFILES` switch (`bedside`, `infusion`,
  `imaging`, `pharmacy`, `ehr`, `facility`) — adding a new asset group is a one-block edit
  to `sim.py` plus a new compose service. Imaging rotates modalities (MRI 3T / MRI 1.5T /
  CT 64 / X-ray DR) by index; pharmacy rotates subtypes (vaccine_freezer / vaccine_refrigerator /
  blood_bank / controlled_substance_safe) so cold-chain and DEA stories coexist; EHR rotates
  channel (ehr_workstation / clinical_kiosk / nurse_mobile / provider_laptop).
- **Compliance / safety note:** the simulator does not store, transmit, or display real PHI.
  MRN, employee IDs, NDC codes, accession numbers, and order IDs are randomly generated and
  bear no relationship to any real patient, provider, drug, study, or order.
