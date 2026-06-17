"""device-sim — unified per-asset-group simulator emitting logs + traces + metrics via OTLP.

One image, six compose services (one per asset group: bedside, infusion, imaging, pharmacy,
ehr, facility). Each container simulates DEVICE_COUNT assets in its group. For EACH asset,
a dedicated OTel Resource + LoggerProvider + TracerProvider is built so logs and traces
carry IDENTICAL resource attributes, enabling Dynatrace to correlate them into a single
entity per asset.

Metrics are emitted with the OpenTelemetry Metrics SDK over OTLP gRPC to the same edge
collector endpoint used by logs and traces.

Env vars:
  GROUP                     asset group (bedside|infusion|imaging|pharmacy|ehr|facility)
  GROUP_NAME                human-readable group label
  ASSET_TYPE                asset type string (e.g. "patient monitor")
  ASSET_VENDOR              vendor string (e.g. "Philips")
  DEVICE_COUNT              number of assets to simulate per container (default 3)
  OTEL_EXPORTER_OTLP_ENDPOINT   gRPC endpoint for the edge collector (e.g. bedside:4317)
  SCRAPE_INTERVAL_S         metric publish interval in seconds (default 30)
  FAILURE_RATE              fault injection rate 0.0-1.0 (default 0.08)
  INTENSITY                 1=minimal 2=standard 3=high (default 2)
Signal contract:
  METRICS  — OTel metrics over OTLP gRPC to edge collector.
  LOGS     — Hospital-style structured log records via OTel Logs SDK → edge collector.
  TRACES   — "medication_administration" spans (scheduled | prn | stat | continuous)
             order_received -> verify_5_rights -> pull_from_adc ->
             barcode_scan_patient -> barcode_scan_drug -> pump_program -> pump_start ->
             infusion_complete (or alarm_occlusion -> notify_clinician on fail) → edge collector.
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
import threading
import uuid

# ── OpenTelemetry imports ────────────────────────────────────────────────────
from opentelemetry import trace as otel_trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.trace import Status, StatusCode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
_root_log = logging.getLogger("device-sim")

# ── Configuration ────────────────────────────────────────────────────────────
GROUP = os.environ.get("GROUP", "bedside")
GROUP_NAME = os.environ.get("GROUP_NAME", GROUP)
ASSET_TYPE = os.environ.get("ASSET_TYPE", "patient monitor")
ASSET_VENDOR = os.environ.get("ASSET_VENDOR", "Philips")
DEVICE_COUNT = int(os.environ.get("DEVICE_COUNT", "3"))
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "bedside:4317")
SCRAPE_INTERVAL_S = float(os.environ.get("SCRAPE_INTERVAL_S", "30"))
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0.08"))
INTENSITY = int(os.environ.get("INTENSITY", "2"))
REGION = "region-east-1"
HEALTH_SYSTEM = "HealthSystem-A"

# ── Asset profiles ───────────────────────────────────────────────────────────
# Each profile selects which metric families to emit and which logs to weight up.
# Per-asset numerical baselines are randomized in DeviceSimulator.__init__.
PROFILES = {
    "bedside": {
        "asset_prefix": "bed",
        "emit": {"bedside": True},
        "log_set": "bedside",
    },
    "infusion": {
        "asset_prefix": "pump",
        "emit": {"infusion": True},
        "log_set": "infusion",
    },
    "imaging": {
        "asset_prefix": "mod",
        "emit": {"imaging": True},
        "log_set": "imaging",
    },
    "pharmacy": {
        "asset_prefix": "adc",
        "emit": {"pharmacy": True},
        "log_set": "pharmacy",
    },
    "ehr": {
        "asset_prefix": "wks",
        "emit": {"ehr": True},
        "log_set": "ehr",
    },
    "facility": {
        "asset_prefix": "bms",
        "emit": {"facility": True},
        "log_set": "facility",
    },
}

# ── Pharmacy fridge / safe sub-types (rotated when GROUP=pharmacy) ───────────
# Each represents a cold-chain or controlled-substance category with its own setpoint.
# Vaccine ultra-cold (-25C) = mRNA biologics; refrigerator (2-8C) = standard vaccines;
# blood bank (1-6C) = packed RBCs; controlled substance safe = room-temp DEA Schedule II-V.
PHARMACY_SUBTYPES = [
    ("vaccine_freezer",            -25.0, -15.0),  # ultra-cold biologics (mRNA / monoclonal Ab)
    ("vaccine_refrigerator",        2.0,   8.0),   # routine vaccines (flu, MMR, hepB)
    ("blood_bank",                  1.0,   6.0),   # packed red blood cells
    ("controlled_substance_safe",  20.0,  25.0),   # DEA Schedule II-V room temp safe
]

# ── EHR channel rotation (when GROUP=ehr) ────────────────────────────────────
EHR_CHANNELS = ["ehr_workstation", "clinical_kiosk", "nurse_mobile", "provider_laptop"]

# ── Imaging modality rotation (when GROUP=imaging) ───────────────────────────
# (modality, magnet_field_T, has_helium)
IMAGING_MODALITIES = [
    ("MRI_3T",   3.0,  True),
    ("MRI_1_5T", 1.5,  True),
    ("CT_64",    0.0,  False),
    ("XRAY_DR",  0.0,  False),
]

# ── Hospital units (random assignment per asset) ─────────────────────────────
HOSPITAL_UNITS = ["ICU", "ED", "OR", "ONC", "MED-SURG", "PEDS", "L-D", "RAD", "PHARM", "TELE"]


# ── Helpers for log message content ─────────────────────────────────────────
def _ip():
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def _facility_id():
    return f"facility-{random.randint(1, 25):03d}"


def _unit():
    return random.choice(HOSPITAL_UNITS)


def _patient_id():
    # MRN-style — fully synthetic, no real patients
    return f"MRN-{random.randint(10000000, 99999999)}"


def _order_id():
    return f"ORD-{uuid.uuid4().hex[:10].upper()}"


def _drug():
    return random.choice([
        "morphine_sulfate", "fentanyl", "vancomycin", "piperacillin_tazobactam",
        "cisplatin", "doxorubicin", "heparin", "warfarin",
        "norepinephrine", "epinephrine", "insulin_regular", "potassium_chloride",
        "metoprolol", "furosemide", "acetaminophen", "ondansetron",
    ])


def _drug_class():
    return random.choice([
        "opioid", "antibiotic", "chemotherapy", "anticoagulant",
        "vasopressor", "electrolyte", "insulin", "antiemetic", "analgesic",
    ])


def _decline_reason():
    return random.choice([
        "bcma_patient_mismatch", "bcma_drug_mismatch", "dose_out_of_range_low",
        "dose_out_of_range_high", "allergy_alert", "drug_drug_interaction",
        "rate_out_of_library", "channel_locked",
    ])


# Each tuple: (severity_name, log_level, event_type, message_fn).

_BEDSIDE_LOGS = [
    ("INFO",     logging.INFO,    "vitals_normal",
     lambda: f"VITALS-NORMAL: pt={_patient_id()} unit={_unit()} hr={random.randint(58,92)} spo2={random.randint(94,99)} bp={random.randint(105,135)}/{random.randint(62,85)}"),
    ("INFO",     logging.INFO,    "patient_admitted",
     lambda: f"PATIENT-ADMITTED: pt={_patient_id()} unit={_unit()} bed={random.randint(1,32):02d}"),
    ("INFO",     logging.INFO,    "rhythm_change",
     lambda: f"RHYTHM-CHANGE: pt={_patient_id()} unit={_unit()} from={random.choice(['NSR','SR'])} to={random.choice(['SR','NSR','SB'])}"),
    ("WARNING",  logging.WARNING, "lead_off",
     lambda: f"LEAD-OFF: pt={_patient_id()} unit={_unit()} lead={random.choice(['I','II','III','V1','V5'])} duration_s={random.randint(20,180)}"),
    ("WARNING",  logging.WARNING, "battery_low",
     lambda: f"BATTERY-LOW: monitor=bed-{random.randint(1,32):02d} pct={random.randint(8,18)}"),
    ("WARNING",  logging.WARNING, "signal_noise_high",
     lambda: f"SIGNAL-NOISE-HIGH: pt={_patient_id()} unit={_unit()} lead={random.choice(['II','V1','V5'])} snr_db={random.uniform(4.0,8.0):.1f}"),
    ("ERROR",    logging.ERROR,   "arrhythmia_detected",
     lambda: f"ARRHYTHMIA-DETECTED: pt={_patient_id()} unit={_unit()} type={random.choice(['AFIB','VT','SVT','PVC-couplet','AFLUT'])} hr_bpm={random.randint(120,180)}"),
    ("ERROR",    logging.ERROR,   "apnea_alarm",
     lambda: f"APNEA-ALARM: pt={_patient_id()} unit={_unit()} duration_s={random.randint(20,45)} spo2_pct={random.randint(82,88)}"),
    ("CRITICAL", logging.CRITICAL, "cardiac_arrest_alarm",
     lambda: f"CARDIAC-ARREST-ALARM: pt={_patient_id()} unit={_unit()} rhythm={random.choice(['VF','asystole','PEA'])} code_blue=true"),
]

_INFUSION_LOGS = [
    ("INFO",     logging.INFO,    "dose_start",
     lambda: f"DOSE-START: pt={_patient_id()} drug={_drug()} rate_ml_hr={random.randint(5,250)} order={_order_id()}"),
    ("INFO",     logging.INFO,    "dose_complete",
     lambda: f"DOSE-COMPLETE: pt={_patient_id()} drug={_drug()} volume_ml={random.randint(25,500)} duration_min={random.randint(15,240)}"),
    ("INFO",     logging.INFO,    "battery_charged",
     lambda: f"BATTERY-CHARGED: pump=pump-{random.randint(1,32):02d} pct=100"),
    ("WARNING",  logging.WARNING, "downstream_occlusion_warn",
     lambda: f"DOWNSTREAM-OCCLUSION-WARN: pump=pump-{random.randint(1,32):02d} pressure_psi={random.uniform(8.0,12.0):.1f}"),
    ("WARNING",  logging.WARNING, "battery_low",
     lambda: f"BATTERY-LOW: pump=pump-{random.randint(1,32):02d} pct={random.randint(8,18)}"),
    ("WARNING",  logging.WARNING, "infusion_paused_user",
     lambda: f"INFUSION-PAUSED-USER: pt={_patient_id()} pump=pump-{random.randint(1,32):02d} elapsed_s={random.randint(30,600)}"),
    ("ERROR",    logging.ERROR,   "occlusion_alarm",
     lambda: f"OCCLUSION-ALARM: pt={_patient_id()} pump=pump-{random.randint(1,32):02d} pressure_psi={random.uniform(15.0,22.0):.1f} drug={_drug()}"),
    ("ERROR",    logging.ERROR,   "air_in_line_alarm",
     lambda: f"AIR-IN-LINE-ALARM: pt={_patient_id()} pump=pump-{random.randint(1,32):02d} bubble_size_ul={random.randint(50,400)}"),
    ("ERROR",    logging.ERROR,   "drug_library_outdated",
     lambda: f"DRUG-LIBRARY-OUTDATED: pump=pump-{random.randint(1,32):02d} current_version={random.randint(20,24)} required_version={random.randint(25,28)}"),
    ("CRITICAL", logging.CRITICAL, "bcma_mismatch_pump_abort",
     lambda: f"BCMA-MISMATCH-PUMP-ABORT: scanned_pt={_patient_id()} ordered_pt={_patient_id()} drug={_drug()} aborted=true patient_safety=true"),
]

_IMAGING_LOGS = [
    ("INFO",     logging.INFO,    "study_complete",
     lambda: f"STUDY-COMPLETE: modality={random.choice(['MRI_3T','CT_64','XRAY_DR'])} accession={uuid.uuid4().hex[:10].upper()} duration_min={random.randint(8,45)}"),
    ("INFO",     logging.INFO,    "study_routed_pacs",
     lambda: f"STUDY-ROUTED-PACS: accession={uuid.uuid4().hex[:10].upper()} images={random.randint(80,2400)} mb={random.randint(20,1800)}"),
    ("INFO",     logging.INFO,    "recon_complete",
     lambda: f"RECON-COMPLETE: accession={uuid.uuid4().hex[:10].upper()} slices={random.randint(120,1200)} duration_s={random.randint(45,420)}"),
    ("WARNING",  logging.WARNING, "study_slow",
     lambda: f"STUDY-SLOW: modality={random.choice(['MRI_3T','CT_64'])} accession={uuid.uuid4().hex[:10].upper()} duration_min={random.randint(60,120)} threshold_min=45"),
    ("WARNING",  logging.WARNING, "pacs_queue_high",
     lambda: f"PACS-QUEUE-HIGH: depth={random.randint(80,250)} threshold=50 oldest_age_min={random.randint(15,60)}"),
    ("WARNING",  logging.WARNING, "recon_retry",
     lambda: f"RECON-RETRY: accession={uuid.uuid4().hex[:10].upper()} attempt={random.randint(2,4)} reason={random.choice(['gpu_timeout','memory_pressure'])}"),
    ("ERROR",    logging.ERROR,   "dicom_study_timeout",
     lambda: f"DICOM-STUDY-TIMEOUT: modality={random.choice(['MRI_3T','CT_64','XRAY_DR'])} accession={uuid.uuid4().hex[:10].upper()} elapsed_s={random.randint(60,180)}"),
    ("ERROR",    logging.ERROR,   "modality_offline",
     lambda: f"MODALITY-OFFLINE: modality={random.choice(['MRI_3T','CT_64','XRAY_DR'])} unit={_unit()} duration_min={random.randint(10,90)}"),
    ("ERROR",    logging.ERROR,   "recon_fail",
     lambda: f"RECON-FAIL: accession={uuid.uuid4().hex[:10].upper()} reason={random.choice(['cuda_oom','corrupt_kspace','licence_invalid'])}"),
    ("CRITICAL", logging.CRITICAL, "magnet_quench_warning",
     lambda: f"MAGNET-QUENCH-WARNING: modality=MRI_3T helium_pct={random.uniform(8.0,18.0):.1f} cryo_pressure_psi={random.uniform(2.5,4.5):.2f} service_required=true"),
]

# Pharmacy: vaccine cold-chain excursion + DEA controlled-substance diversion
# are the SIGNATURE events of this demo — life-safety + regulatory.
_PHARMACY_LOGS = [
    ("INFO",     logging.INFO,    "adc_dispense",
     lambda: f"ADC-DISPENSE: cabinet=adc-{random.randint(1,24):02d} drug={_drug()} qty={random.randint(1,4)} rn=emp-{random.randint(1000,9999)} pt={_patient_id()}"),
    ("INFO",     logging.INFO,    "vaccine_cold_chain_normal",
     lambda: f"VACCINE-COLD-CHAIN-NORMAL: unit=adc-{random.randint(1,24):02d} temp_c={random.uniform(-22.0,7.0):.2f} setpoint_c={random.choice([-20.0,5.0])}"),
    ("INFO",     logging.INFO,    "replenish_complete",
     lambda: f"REPLENISH-COMPLETE: cabinet=adc-{random.randint(1,24):02d} drug={_drug()} qty={random.randint(10,80)} pharmacist=emp-{random.randint(1000,9999)}"),
    ("WARNING",  logging.WARNING, "stock_low",
     lambda: f"STOCK-LOW: cabinet=adc-{random.randint(1,24):02d} drug={_drug()} on_hand={random.randint(1,5)} par_level={random.randint(8,24)}"),
    ("WARNING",  logging.WARNING, "compressor_high_amps",
     lambda: f"COMPRESSOR-HIGH-AMPS: unit=adc-{random.randint(1,24):02d} amps={random.uniform(18.0,24.0):.2f} threshold_amps=16.0"),
    ("WARNING",  logging.WARNING, "door_left_open",
     lambda: f"DOOR-LEFT-OPEN: unit=adc-{random.randint(1,24):02d} duration_s={random.randint(60,600)} threshold_s=45"),
    ("ERROR",    logging.ERROR,   "vaccine_cold_chain_excursion",
     lambda: f"VACCINE-COLD-CHAIN-EXCURSION: unit=adc-{random.randint(1,24):02d} temp_c={random.uniform(10.0,16.0):.2f} setpoint_c=5.0 duration_min={random.randint(15,90)} doses_at_risk={random.randint(40,400)}"),
    ("ERROR",    logging.ERROR,   "dea_discrepancy",
     lambda: f"DEA-DISCREPANCY: cabinet=adc-{random.randint(1,24):02d} drug={_drug()} expected_count={random.randint(20,80)} actual_count={random.randint(0,40)} schedule={random.choice(['II','III'])}"),
    ("CRITICAL", logging.CRITICAL, "vaccine_doses_at_risk",
     lambda: f"VACCINE-DOSES-AT-RISK: unit=adc-{random.randint(1,24):02d} temp_c={random.uniform(14.0,22.0):.2f} duration_min={random.randint(120,360)} doses_at_risk={random.randint(200,1800)} est_loss_usd={random.uniform(8000,85000):.2f} hold_inventory=true"),
    ("CRITICAL", logging.CRITICAL, "controlled_substance_diversion_alert",
     lambda: f"CONTROLLED-SUBSTANCE-DIVERSION-ALERT: cabinet=adc-{random.randint(1,24):02d} drug={_drug()} rn=emp-{random.randint(1000,9999)} anomaly_score={random.uniform(85,99):.1f} dea_notify=required"),
]

_EHR_LOGS = [
    ("INFO",     logging.INFO,    "chart_open",
     lambda: f"CHART-OPEN: pt={_patient_id()} user=emp-{random.randint(1000,9999)} unit={_unit()} latency_ms={random.randint(180,650)}"),
    ("INFO",     logging.INFO,    "med_order_placed",
     lambda: f"MED-ORDER-PLACED: pt={_patient_id()} drug={_drug()} dose={random.choice(['25mg','50mg','100mg','5units','10mEq'])} order={_order_id()}"),
    ("INFO",     logging.INFO,    "result_signed",
     lambda: f"RESULT-SIGNED: pt={_patient_id()} test={random.choice(['CBC','BMP','TROP-I','LACTATE','BLOOD-CX'])} signer=emp-{random.randint(1000,9999)}"),
    ("WARNING",  logging.WARNING, "chart_open_slow",
     lambda: f"CHART-OPEN-SLOW: pt={_patient_id()} user=emp-{random.randint(1000,9999)} latency_ms={random.randint(2500,6000)} threshold_ms=1500"),
    ("WARNING",  logging.WARNING, "login_storm_detected",
     lambda: f"LOGIN-STORM-DETECTED: unit={_unit()} attempts_per_min={random.randint(80,240)} cause={random.choice(['shift_change','sso_outage'])}"),
    ("WARNING",  logging.WARNING, "session_timeout_burst",
     lambda: f"SESSION-TIMEOUT-BURST: unit={_unit()} count={random.randint(15,60)} window_min=5"),
    ("ERROR",    logging.ERROR,   "ehr_5xx",
     lambda: f"EHR-5XX: route=/chart/{random.choice(['orders','results','meds','notes'])} status={random.choice([500,502,503,504])} request_id=req-{uuid.uuid4().hex[:10]}"),
    ("ERROR",    logging.ERROR,   "interface_engine_lag",
     lambda: f"INTERFACE-ENGINE-LAG: queue={random.choice(['ADT','ORM','ORU','SIU'])} depth={random.randint(800,5000)} threshold=200 oldest_age_s={random.randint(60,900)}"),
    ("CRITICAL", logging.CRITICAL, "ehr_outage_downtime_procedure",
     lambda: f"EHR-OUTAGE-DOWNTIME-PROCEDURE: affected_units={random.randint(4,18)} duration_s={random.randint(120,3600)} paper_charting=true revenue_at_risk=true"),
]

_FACILITY_LOGS = [
    ("INFO",     logging.INFO,    "or_pressure_normal",
     lambda: f"OR-PRESSURE-NORMAL: room=OR-{random.randint(1,18):02d} positive_inwc={random.uniform(0.020,0.040):.4f} ach={random.randint(20,28)}"),
    ("INFO",     logging.INFO,    "iso_pressure_normal",
     lambda: f"ISO-PRESSURE-NORMAL: room=ISO-{random.randint(1,24):02d} negative_inwc={random.uniform(-0.040,-0.020):.4f} ach={random.randint(12,18)}"),
    ("INFO",     logging.INFO,    "hvac_zone_normal",
     lambda: f"HVAC-ZONE-NORMAL: zone={random.randint(1,32)} unit={_unit()} temp_c={random.uniform(21.0,23.5):.2f} setpoint_c=22.0"),
    ("WARNING",  logging.WARNING, "nurse_call_delay",
     lambda: f"NURSE-CALL-DELAY: unit={_unit()} room={random.randint(1,32):02d} ack_delay_s={random.randint(180,420)} threshold_s=120"),
    ("WARNING",  logging.WARNING, "hvac_setpoint_drift",
     lambda: f"HVAC-SETPOINT-DRIFT: zone={random.randint(1,32)} unit={_unit()} actual_c={random.uniform(24.5,27.5):.2f} setpoint_c=22.0"),
    ("WARNING",  logging.WARNING, "or_pressure_low_warn",
     lambda: f"OR-PRESSURE-LOW-WARN: room=OR-{random.randint(1,18):02d} positive_inwc={random.uniform(0.005,0.015):.4f} threshold_inwc=0.018"),
    ("ERROR",    logging.ERROR,   "or_positive_pressure_lost",
     lambda: f"OR-POSITIVE-PRESSURE-LOST: room=OR-{random.randint(1,18):02d} positive_inwc={random.uniform(-0.005,0.003):.4f} duration_min={random.randint(5,30)} case_status=under_review infection_control_notify=true"),
    ("ERROR",    logging.ERROR,   "nurse_call_unanswered",
     lambda: f"NURSE-CALL-UNANSWERED: unit={_unit()} room={random.randint(1,32):02d} duration_s={random.randint(420,1200)} escalation=charge_nurse"),
    ("CRITICAL", logging.CRITICAL, "iso_negative_pressure_lost",
     lambda: f"ISO-NEGATIVE-PRESSURE-LOST: room=ISO-{random.randint(1,24):02d} negative_inwc={random.uniform(-0.005,0.010):.4f} pathogen={random.choice(['M.tuberculosis','SARS-CoV-2','measles','C.auris'])} airborne_exposure_risk=true"),
]

LOG_SETS = {
    "bedside": _BEDSIDE_LOGS,
    "infusion": _INFUSION_LOGS,
    "imaging": _IMAGING_LOGS,
    "pharmacy": _PHARMACY_LOGS,
    "ehr": _EHR_LOGS,
    "facility": _FACILITY_LOGS,
}

# Default severity weights match the order of templates in each set above.
LOG_WEIGHTS = {
    "bedside":  [70, 25, 30, 20, 14, 12, 6, 4, 1],
    "infusion": [65, 35, 25, 22, 14, 18, 8, 5, 3, 1],
    "imaging":  [55, 40, 35, 22, 18, 14, 8, 5, 3, 1],
    "pharmacy": [60, 40, 30, 22, 16, 14, 6, 4, 1, 1],
    "ehr":      [70, 45, 35, 22, 16, 18, 8, 6, 1],
    "facility": [60, 45, 70, 18, 16, 14, 5, 6, 1],
}

# ── Trace operation types ────────────────────────────────────────────────────
OPERATION_TYPES = ["scheduled_dose", "prn_dose", "stat_dose", "continuous_infusion"]
MEDICATION_CLASSES = ["opioid", "antibiotic", "chemotherapy", "electrolyte",
                      "anticoagulant", "vasopressor", "insulin", "antiemetic", "analgesic"]


def _dim_safe(s: str) -> str:
    """Sanitise a string for use as a metric attribute value."""
    return s.lower().replace(" ", "-").replace("/", "-").replace(",", "").replace("_", "-").replace("&", "and")


# ─────────────────────────────────────────────────────────────────────────────
# Per-asset simulator class
# ─────────────────────────────────────────────────────────────────────────────

class DeviceSimulator:
    """Holds OTel log + trace + meter providers for one simulated hospital asset."""

    def __init__(self, device_id: str, profile: dict, pharmacy_subtype: tuple | None = None,
                 ehr_channel: str | None = None, imaging_modality: tuple | None = None):
        self.device_id = device_id
        self.profile = profile
        self.pharmacy_subtype = pharmacy_subtype  # (name, min_c, max_c)
        self.ehr_channel = ehr_channel
        self.imaging_modality = imaging_modality  # (modality, field_T, has_helium)
        # Every asset belongs to a (simulated) facility + unit — gives Dynatrace a hospital dim.
        self.facility_id = _facility_id()
        self.unit = _unit()

        # Shared resource attributes — IDENTICAL across logs, traces, and metrics.
        attrs = {
            "service.namespace": "healthcare",
            "service.name": device_id,
            "host.name": device_id,
            "asset.id": device_id,
            "asset.type": ASSET_TYPE,
            "asset.vendor": ASSET_VENDOR,
            "healthcare.assetgroup": GROUP,
            "healthcare.region": REGION,
            "healthcare.facility_id": self.facility_id,
            "healthcare.unit": self.unit,
            "healthcare.health_system": HEALTH_SYSTEM,
        }
        if pharmacy_subtype is not None:
            attrs["healthcare.pharmacy.subtype"] = pharmacy_subtype[0]
            attrs["healthcare.pharmacy.setpoint_c"] = (pharmacy_subtype[1] + pharmacy_subtype[2]) / 2.0
        if ehr_channel is not None:
            attrs["healthcare.ehr.channel"] = ehr_channel
        if imaging_modality is not None:
            attrs["healthcare.imaging.modality"] = imaging_modality[0]
            attrs["healthcare.imaging.magnet_field_t"] = imaging_modality[1]
        resource = Resource.create(attrs)

        endpoint = OTLP_ENDPOINT

        # ── Trace provider ────────────────────────────────────────────────
        trace_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        self._tracer_provider = TracerProvider(resource=resource)
        self._tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        self._tracer = self._tracer_provider.get_tracer(f"device-sim.{device_id}")

        # ── Log provider ──────────────────────────────────────────────────
        log_exporter = OTLPLogExporter(endpoint=endpoint, insecure=True)
        self._logger_provider = LoggerProvider(resource=resource)
        self._logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        otel_handler = LoggingHandler(
            level=logging.DEBUG, logger_provider=self._logger_provider
        )
        self._logger = logging.getLogger(f"asset.{device_id}")
        self._logger.setLevel(logging.DEBUG)
        self._logger.propagate = False
        self._logger.addHandler(otel_handler)

        # ── Meter provider ────────────────────────────────────────────────
        metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
        metric_reader = PeriodicExportingMetricReader(
            exporter=metric_exporter,
            export_interval_millis=max(1000, int(SCRAPE_INTERVAL_S * 1000)),
        )
        self._meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        self._meter = self._meter_provider.get_meter(f"device-sim.{device_id}")

        # ── Bedside instruments ───────────────────────────────────────────
        self._m_bed_hr        = self._meter.create_histogram("healthcare.bedside.heart_rate_bpm")
        self._m_bed_spo2      = self._meter.create_histogram("healthcare.bedside.spo2_pct")
        self._m_bed_bp_sys    = self._meter.create_histogram("healthcare.bedside.bp_systolic_mmhg")
        self._m_bed_bp_dia    = self._meter.create_histogram("healthcare.bedside.bp_diastolic_mmhg")
        self._c_bed_alarms    = self._meter.create_counter("healthcare.bedside.alarm_count")

        # ── Infusion instruments ──────────────────────────────────────────
        self._m_inf_rate      = self._meter.create_histogram("healthcare.infusion.rate_ml_per_hr")
        self._m_inf_occlusion = self._meter.create_histogram("healthcare.infusion.occlusion_pressure_psi")
        self._m_inf_battery   = self._meter.create_histogram("healthcare.infusion.battery_pct")
        self._c_inf_dose_err  = self._meter.create_counter("healthcare.infusion.dose_error_count")
        self._m_inf_lib_ver   = self._meter.create_histogram("healthcare.infusion.drug_library_version")

        # ── Imaging instruments ───────────────────────────────────────────
        self._m_img_studies   = self._meter.create_histogram("healthcare.imaging.studies_per_hour")
        self._m_img_acq_ms    = self._meter.create_histogram("healthcare.imaging.acquisition_latency_ms")
        self._m_img_helium    = self._meter.create_histogram("healthcare.imaging.magnet_helium_pct")
        self._m_img_tube_c    = self._meter.create_histogram("healthcare.imaging.tube_temp_c")
        self._m_img_queue     = self._meter.create_histogram("healthcare.imaging.queue_depth")

        # ── Pharmacy / cold-chain instruments ─────────────────────────────
        self._m_phar_temp     = self._meter.create_histogram("healthcare.pharmacy.fridge_temp_c")
        self._m_phar_amps     = self._meter.create_histogram("healthcare.pharmacy.compressor_amps")
        self._c_phar_access   = self._meter.create_counter("healthcare.pharmacy.access_count")
        self._c_phar_divert   = self._meter.create_counter("healthcare.pharmacy.diversion_alert_count")
        self._m_phar_stock    = self._meter.create_histogram("healthcare.pharmacy.stock_pct")

        # ── EHR instruments ───────────────────────────────────────────────
        self._m_ehr_chart_lat = self._meter.create_histogram("healthcare.ehr.chart_open_latency_ms")
        self._c_ehr_logins    = self._meter.create_counter("healthcare.ehr.login_count")
        self._m_ehr_sessions  = self._meter.create_histogram("healthcare.ehr.session_count")
        self._m_ehr_search    = self._meter.create_histogram("healthcare.ehr.search_latency_ms")
        self._m_ehr_unsigned  = self._meter.create_histogram("healthcare.ehr.unsigned_orders_count")

        # ── Facility / BMS instruments ────────────────────────────────────
        self._m_fac_or_pres   = self._meter.create_histogram("healthcare.or.positive_pressure_inwc")
        self._m_fac_or_ach    = self._meter.create_histogram("healthcare.or.air_changes_per_hour")
        self._m_fac_iso_pres  = self._meter.create_histogram("healthcare.iso.negative_pressure_inwc")
        self._m_fac_zone_c    = self._meter.create_histogram("healthcare.hvac.zone_temp_c")
        self._m_fac_nc_active = self._meter.create_histogram("healthcare.nurse_call.active_count")

        # ── Per-asset simulated state baselines ───────────────────────────
        self._cycle_offset = random.uniform(0, 2 * math.pi)
        # bedside
        self._bed_hr = random.uniform(62.0, 88.0)
        self._bed_spo2 = random.uniform(95.0, 99.0)
        self._bed_bp_sys = random.uniform(108.0, 132.0)
        self._bed_bp_dia = random.uniform(65.0, 82.0)
        self._bed_alarm_total = 0
        # infusion
        self._inf_rate = random.uniform(25.0, 180.0)
        self._inf_occlusion = random.uniform(2.0, 6.0)
        self._inf_battery = random.uniform(40.0, 95.0)
        self._inf_dose_err_total = 0
        self._inf_lib_ver = random.choice([24, 25, 26, 27, 28])
        # imaging
        self._img_studies = random.uniform(2.0, 8.0)
        self._img_acq_ms = random.uniform(2500.0, 9000.0)
        if imaging_modality is not None and imaging_modality[2]:
            self._img_helium = random.uniform(82.0, 98.0)
        else:
            self._img_helium = 100.0  # non-MRI modalities: no helium
        self._img_tube_c = random.uniform(36.0, 52.0)
        self._img_queue = random.uniform(4.0, 28.0)
        # pharmacy / cold chain
        if pharmacy_subtype is not None:
            sub_name, sub_min, sub_max = pharmacy_subtype
            self._phar_setpoint = (sub_min + sub_max) / 2.0
        else:
            self._phar_setpoint = 5.0
        self._phar_temp = self._phar_setpoint + random.uniform(-0.5, 0.5)
        self._phar_amps = random.uniform(9.0, 15.0)
        self._phar_access_total = 0
        self._phar_divert_total = 0
        self._phar_stock = random.uniform(65.0, 96.0)
        # ehr
        self._ehr_chart_lat = random.uniform(280.0, 720.0)
        self._ehr_logins_total = 0
        self._ehr_sessions = random.uniform(40.0, 220.0)
        self._ehr_search = random.uniform(120.0, 420.0)
        self._ehr_unsigned = random.uniform(2.0, 18.0)
        # facility
        self._fac_or_pres = random.uniform(0.022, 0.036)
        self._fac_or_ach = random.uniform(22.0, 28.0)
        self._fac_iso_pres = random.uniform(-0.034, -0.022)
        self._fac_zone_c = random.uniform(21.6, 22.6)
        self._fac_nc_active = random.uniform(2.0, 14.0)

        self._prev_bed_alarms = 0
        self._prev_inf_dose_err = 0
        self._prev_phar_access = 0
        self._prev_phar_divert = 0
        self._prev_ehr_logins = 0

    # ── Simulation tick ───────────────────────────────────────────────────
    def tick(self, t: float):
        """Update simulated values for the current time step t (monotonic seconds)."""
        phase = t / 300.0 + self._cycle_offset
        fault = random.random() < FAILURE_RATE

        # ── Bedside ────────────────────────────────────────────────────────
        # Mild circadian variation + occasional alarm bursts on fault.
        baseline_hr = 76.0 + 8.0 * math.sin(phase * 0.6)
        self._bed_hr = max(35.0,
            0.7 * self._bed_hr + 0.3 * baseline_hr + random.gauss(0, 2.5)
            + (random.uniform(30.0, 70.0) if (fault and GROUP == "bedside") else 0.0)
        )
        self._bed_spo2 = max(60.0, min(100.0,
            97.5 + random.gauss(0, 0.4)
            - (random.uniform(6.0, 14.0) if (fault and GROUP == "bedside") else 0.0)
        ))
        self._bed_bp_sys = max(60.0,
            120.0 + 8.0 * math.sin(phase * 0.5) + random.gauss(0, 4.0)
            + (random.uniform(-25.0, 25.0) if (fault and GROUP == "bedside") else 0.0)
        )
        self._bed_bp_dia = max(40.0, self._bed_bp_sys * random.uniform(0.6, 0.72))
        if fault and GROUP == "bedside" and random.random() < 0.55:
            self._bed_alarm_total += random.randint(1, 4)

        # ── Infusion ───────────────────────────────────────────────────────
        target_rate = 80.0 + 30.0 * math.sin(phase * 0.4)
        self._inf_rate = max(0.0,
            0.7 * self._inf_rate + 0.3 * target_rate + random.gauss(0, 3.0)
        )
        baseline_occ = 4.0 + 1.0 * math.sin(phase * 1.1)
        self._inf_occlusion = max(0.0,
            0.7 * self._inf_occlusion + 0.3 * baseline_occ + random.gauss(0, 0.4)
            + (random.uniform(8.0, 18.0) if (fault and GROUP == "infusion" and random.random() < 0.5) else 0.0)
        )
        self._inf_battery = max(0.0, min(100.0,
            self._inf_battery - random.uniform(0.05, 0.2)
            + (random.uniform(15.0, 35.0) if random.random() < 0.02 else 0.0)  # plug-ins
        ))
        if fault and GROUP == "infusion" and random.random() < 0.4:
            self._inf_dose_err_total += random.randint(1, 2)
        # drug library version stays put unless we simulate an update
        if random.random() < 0.001:
            self._inf_lib_ver = min(30, self._inf_lib_ver + 1)

        # ── Imaging ────────────────────────────────────────────────────────
        baseline_studies = 5.5 + 2.0 * max(0.0, math.sin(phase * 0.4))
        self._img_studies = max(0.0,
            0.7 * self._img_studies + 0.3 * baseline_studies + random.gauss(0, 0.5)
            - (random.uniform(1.0, 4.0) if (fault and GROUP == "imaging") else 0.0)
        )
        self._img_acq_ms = max(500.0,
            0.7 * self._img_acq_ms + 0.3 * (4500.0 + 1500.0 * math.sin(phase * 0.8))
            + random.gauss(0, 250.0)
            + (random.uniform(8000.0, 30000.0) if (fault and GROUP == "imaging") else 0.0)
        )
        # MRI helium boils off slowly; non-MRI stays at 100.
        if self.imaging_modality is not None and self.imaging_modality[2]:
            self._img_helium = max(0.0,
                self._img_helium - random.uniform(0.0, 0.02)
                - (random.uniform(2.0, 6.0) if (fault and GROUP == "imaging" and random.random() < 0.3) else 0.0)
            )
        self._img_tube_c = max(20.0, min(85.0,
            42.0 + 6.0 * math.sin(phase * 1.2) + random.gauss(0, 1.5)
            + (random.uniform(8.0, 25.0) if (fault and GROUP == "imaging") else 0.0)
        ))
        self._img_queue = max(0.0,
            0.7 * self._img_queue + 0.3 * (12.0 + 8.0 * math.sin(phase * 0.6))
            + random.gauss(0, 2.0)
            + (random.uniform(40.0, 180.0) if (fault and GROUP == "imaging") else 0.0)
        )

        # ── Pharmacy / cold chain ──────────────────────────────────────────
        # Slow thermal drift toward setpoint + occasional excursions on fault.
        self._phar_temp = (self._phar_temp
                           + random.gauss(0, 0.12)
                           - 0.04 * (self._phar_temp - self._phar_setpoint))
        if fault and GROUP == "pharmacy" and self.pharmacy_subtype is not None \
                and self.pharmacy_subtype[0] != "controlled_substance_safe":
            # Temp excursion event — defining demo signal for vaccine cold chain.
            self._phar_temp += random.uniform(2.0, 9.0)
        self._phar_amps = max(0.0,
            12.0 + 2.5 * math.sin(phase * 1.4) + random.gauss(0, 0.5)
            + (random.uniform(4.0, 9.0) if (fault and GROUP == "pharmacy" and random.random() < 0.35) else 0.0)
        )
        # Cabinet access events (per-cycle dispense activity)
        access_this_cycle = random.randint(0, 6)
        if fault and GROUP == "pharmacy":
            access_this_cycle += random.randint(0, 4)
        self._phar_access_total += access_this_cycle
        # DEA diversion alerts: very rare, but heavily weighted on fault
        if fault and GROUP == "pharmacy" and self.pharmacy_subtype is not None \
                and self.pharmacy_subtype[0] == "controlled_substance_safe" \
                and random.random() < 0.25:
            self._phar_divert_total += 1
        self._phar_stock = max(0.0, min(100.0,
            self._phar_stock - (random.uniform(0.3, 1.5) if fault else random.uniform(0.0, 0.4))
            + random.uniform(0.0, 0.6)  # replenishment
        ))

        # ── EHR ────────────────────────────────────────────────────────────
        baseline_chart = 480.0 + 120.0 * max(0.0, math.sin(phase * 0.9))
        self._ehr_chart_lat = max(50.0,
            0.7 * self._ehr_chart_lat + 0.3 * baseline_chart + random.gauss(0, 35.0)
            + (random.uniform(1200.0, 4500.0) if (fault and GROUP == "ehr") else 0.0)
        )
        # Login storms at shift change (every ~30 cycles)
        logins_this_cycle = max(0, int(8.0 + 4.0 * math.sin(phase * 1.5) + random.gauss(0, 2.0)))
        if fault and GROUP == "ehr" and random.random() < 0.4:
            logins_this_cycle += random.randint(40, 120)
        self._ehr_logins_total += logins_this_cycle
        self._ehr_sessions = max(0.0,
            120.0 + 60.0 * math.sin(phase * 0.7) + random.gauss(0, 8.0)
            - (random.uniform(20.0, 60.0) if (fault and GROUP == "ehr") else 0.0)
        )
        self._ehr_search = max(40.0,
            220.0 + 70.0 * math.sin(phase * 1.0) + random.gauss(0, 25.0)
            + (random.uniform(500.0, 2200.0) if (fault and GROUP == "ehr") else 0.0)
        )
        self._ehr_unsigned = max(0.0,
            10.0 + random.gauss(0, 3.0)
            + (random.uniform(8.0, 35.0) if (fault and GROUP == "ehr") else 0.0)
        )

        # ── Facility / BMS ─────────────────────────────────────────────────
        # OR positive pressure must stay > +0.018 inWC per ASHRAE 170.
        self._fac_or_pres = max(-0.020, min(0.060,
            0.030 + 0.004 * math.sin(phase * 0.4) + random.gauss(0, 0.001)
            - (random.uniform(0.025, 0.050) if (fault and GROUP == "facility" and random.random() < 0.45) else 0.0)
        ))
        self._fac_or_ach = max(8.0,
            24.0 + 2.0 * math.sin(phase * 0.3) + random.gauss(0, 0.6)
        )
        # Isolation negative pressure must stay < -0.01 inWC.
        self._fac_iso_pres = max(-0.060, min(0.030,
            -0.028 + 0.003 * math.sin(phase * 0.4) + random.gauss(0, 0.001)
            + (random.uniform(0.025, 0.050) if (fault and GROUP == "facility" and random.random() < 0.30) else 0.0)
        ))
        self._fac_zone_c = max(15.0, min(28.0,
            22.0 + 0.6 * math.sin(phase * 0.3) + random.gauss(0, 0.2)
            + (random.uniform(2.0, 5.0) if (fault and GROUP == "facility") else 0.0)
        ))
        self._fac_nc_active = max(0.0,
            6.0 + 3.0 * math.sin(phase * 1.2) + random.gauss(0, 1.0)
            + (random.uniform(8.0, 25.0) if (fault and GROUP == "facility") else 0.0)
        )

        _root_log.info(
            "group=%s asset=%s facility=%s unit=%s hr=%.0f spo2=%.0f occlusion=%.1f studies=%.1f temp_c=%.2f chart_ms=%.0f or_pres=%.4f fault=%s",
            GROUP, self.device_id, self.facility_id, self.unit, self._bed_hr, self._bed_spo2,
            self._inf_occlusion, self._img_studies, self._phar_temp, self._ehr_chart_lat,
            self._fac_or_pres, fault,
        )

        # Emit a log record this tick
        self._emit_log(fault)
        # Emit metrics this tick
        self.emit_metrics()

    # ── Metric emission ────────────────────────────────────────────────────
    def emit_metrics(self) -> None:
        asset_attrs = {
            "asset.id": self.device_id,
            "asset.type": _dim_safe(ASSET_TYPE),
            "asset.vendor": _dim_safe(ASSET_VENDOR),
            "healthcare.assetgroup": GROUP,
            "healthcare.region": REGION,
            "healthcare.facility_id": self.facility_id,
            "healthcare.unit": self.unit,
            "healthcare.health_system": HEALTH_SYSTEM,
        }
        if self.pharmacy_subtype is not None:
            asset_attrs["healthcare.pharmacy.subtype"] = self.pharmacy_subtype[0]
        if self.ehr_channel is not None:
            asset_attrs["healthcare.ehr.channel"] = self.ehr_channel
        if self.imaging_modality is not None:
            asset_attrs["healthcare.imaging.modality"] = self.imaging_modality[0]

        emit = self.profile["emit"]

        if emit.get("bedside"):
            self._m_bed_hr.record(round(self._bed_hr, 1), asset_attrs)
            self._m_bed_spo2.record(round(self._bed_spo2, 1), asset_attrs)
            self._m_bed_bp_sys.record(round(self._bed_bp_sys, 1), asset_attrs)
            self._m_bed_bp_dia.record(round(self._bed_bp_dia, 1), asset_attrs)
            delta = max(0, self._bed_alarm_total - self._prev_bed_alarms)
            self._c_bed_alarms.add(delta, asset_attrs)
            self._prev_bed_alarms = self._bed_alarm_total

        if emit.get("infusion"):
            self._m_inf_rate.record(round(self._inf_rate, 2), asset_attrs)
            self._m_inf_occlusion.record(round(self._inf_occlusion, 2), asset_attrs)
            self._m_inf_battery.record(round(self._inf_battery, 1), asset_attrs)
            delta = max(0, self._inf_dose_err_total - self._prev_inf_dose_err)
            self._c_inf_dose_err.add(delta, asset_attrs)
            self._prev_inf_dose_err = self._inf_dose_err_total
            self._m_inf_lib_ver.record(self._inf_lib_ver, asset_attrs)

        if emit.get("imaging"):
            self._m_img_studies.record(round(self._img_studies, 2), asset_attrs)
            self._m_img_acq_ms.record(round(self._img_acq_ms, 1), asset_attrs)
            self._m_img_helium.record(round(self._img_helium, 2), asset_attrs)
            self._m_img_tube_c.record(round(self._img_tube_c, 2), asset_attrs)
            self._m_img_queue.record(round(self._img_queue, 1), asset_attrs)

        if emit.get("pharmacy"):
            self._m_phar_temp.record(round(self._phar_temp, 2), asset_attrs)
            self._m_phar_amps.record(round(self._phar_amps, 2), asset_attrs)
            delta = max(0, self._phar_access_total - self._prev_phar_access)
            self._c_phar_access.add(delta, asset_attrs)
            self._prev_phar_access = self._phar_access_total
            delta = max(0, self._phar_divert_total - self._prev_phar_divert)
            self._c_phar_divert.add(delta, asset_attrs)
            self._prev_phar_divert = self._phar_divert_total
            self._m_phar_stock.record(round(self._phar_stock, 2), asset_attrs)

        if emit.get("ehr"):
            self._m_ehr_chart_lat.record(round(self._ehr_chart_lat, 2), asset_attrs)
            delta = max(0, self._ehr_logins_total - self._prev_ehr_logins)
            self._c_ehr_logins.add(delta, asset_attrs)
            self._prev_ehr_logins = self._ehr_logins_total
            self._m_ehr_sessions.record(round(self._ehr_sessions, 1), asset_attrs)
            self._m_ehr_search.record(round(self._ehr_search, 2), asset_attrs)
            self._m_ehr_unsigned.record(round(self._ehr_unsigned, 1), asset_attrs)

        if emit.get("facility"):
            self._m_fac_or_pres.record(round(self._fac_or_pres, 4), asset_attrs)
            self._m_fac_or_ach.record(round(self._fac_or_ach, 2), asset_attrs)
            self._m_fac_iso_pres.record(round(self._fac_iso_pres, 4), asset_attrs)
            self._m_fac_zone_c.record(round(self._fac_zone_c, 2), asset_attrs)
            self._m_fac_nc_active.record(round(self._fac_nc_active, 1), asset_attrs)

    # ── Log emission ───────────────────────────────────────────────────────
    def _emit_log(self, fault: bool):
        log_set_name = self.profile["log_set"]
        templates = LOG_SETS[log_set_name]
        weights = list(LOG_WEIGHTS[log_set_name])
        if fault:
            # Push severity distribution toward warnings/errors during faults.
            for i, (sev, _, _, _) in enumerate(templates):
                if sev == "WARNING":
                    weights[i] = int(weights[i] * 2.5)
                elif sev in ("ERROR", "CRITICAL"):
                    weights[i] = int(weights[i] * 4.0)
        template = random.choices(templates, weights=weights)[0]
        sev_name, log_level, event_type, msg_fn = template
        message = msg_fn()
        self._logger.log(
            log_level,
            message,
            extra={
                "otelSpanID": "0000000000000000",
                "otelTraceID": "00000000000000000000000000000000",
                "otelTraceSampled": False,
            },
        )
        _root_log.debug(
            "log_emit asset=%s event=%s severity=%s msg=%r",
            self.device_id, event_type, sev_name, message[:80],
        )

    # ── Trace emission ─────────────────────────────────────────────────────
    def emit_trace(self):
        """Emit one medication_administration trace with the canonical clinical span chain:
        order_received -> verify_5_rights -> pull_from_adc -> barcode_scan_patient
          -> barcode_scan_drug -> pump_program -> pump_start
          -> infusion_complete | (alarm_occlusion -> notify_clinician)
        """
        op_type = random.choice(OPERATION_TYPES)
        med_class = random.choice(MEDICATION_CLASSES)
        drug = _drug()
        pt_id = _patient_id()
        order_id = _order_id()
        rate_ml_hr = random.randint(5, 250)
        # Higher abort odds for high-risk drug classes + faults.
        risk_mult = 1.5 if med_class in ("opioid", "chemotherapy", "vasopressor", "anticoagulant") else 1.0
        decline_odds = FAILURE_RATE * risk_mult
        do_fail = random.random() < decline_odds
        fail_stage = random.choice(["bcma_patient", "bcma_drug", "occlusion"]) if do_fail else None

        with self._tracer.start_as_current_span("medication_administration") as root:
            root.set_attribute("order.id", order_id)
            root.set_attribute("operation.type", op_type)
            root.set_attribute("medication.class", med_class)
            root.set_attribute("medication.name", drug)
            root.set_attribute("patient.id", pt_id)
            root.set_attribute("asset.id", self.device_id)
            root.set_attribute("healthcare.assetgroup", GROUP)
            root.set_attribute("healthcare.region", REGION)
            root.set_attribute("healthcare.facility_id", self.facility_id)
            root.set_attribute("healthcare.unit", self.unit)
            root.set_attribute("healthcare.health_system", HEALTH_SYSTEM)
            root.set_attribute("infusion.rate_ml_per_hr", rate_ml_hr)
            time.sleep(random.uniform(0.005, 0.02))

            with self._tracer.start_as_current_span("order_received") as span:
                span.set_attribute("order.id", order_id)
                span.set_attribute("order.source", random.choice(["epic", "cerner", "meditech"]))
                span.set_attribute("ordering.provider", f"prov-{random.randint(1000,9999)}")
                time.sleep(random.uniform(0.02, 0.06))

            with self._tracer.start_as_current_span("verify_5_rights") as span:
                span.set_attribute("order.id", order_id)
                span.set_attribute("five_rights.patient", True)
                span.set_attribute("five_rights.drug", True)
                span.set_attribute("five_rights.dose", True)
                span.set_attribute("five_rights.route", True)
                span.set_attribute("five_rights.time", True)
                time.sleep(random.uniform(0.01, 0.04))

            with self._tracer.start_as_current_span("pull_from_adc") as span:
                span.set_attribute("order.id", order_id)
                span.set_attribute("adc.cabinet", f"adc-{random.randint(1,24):02d}")
                span.set_attribute("adc.witness_required", med_class in ("opioid",))
                time.sleep(random.uniform(0.05, 0.18))

            with self._tracer.start_as_current_span("barcode_scan_patient") as span:
                span.set_attribute("order.id", order_id)
                span.set_attribute("patient.id", pt_id)
                if do_fail and fail_stage == "bcma_patient":
                    span.set_attribute("bcma.match", False)
                    span.set_attribute("bcma.scanned_patient_id", _patient_id())  # wrong patient
                    reason = "bcma_patient_mismatch"
                    span.set_status(Status(StatusCode.ERROR, f"BCMA fail: {reason}"))
                    span.record_exception(RuntimeError(f"order {order_id}: {reason}"))
                else:
                    span.set_attribute("bcma.match", True)
                time.sleep(random.uniform(0.02, 0.06))

            if not (do_fail and fail_stage == "bcma_patient"):
                with self._tracer.start_as_current_span("barcode_scan_drug") as span:
                    span.set_attribute("order.id", order_id)
                    span.set_attribute("medication.name", drug)
                    if do_fail and fail_stage == "bcma_drug":
                        span.set_attribute("bcma.match", False)
                        span.set_attribute("bcma.scanned_ndc", f"NDC-{random.randint(10000,99999)}-{random.randint(100,999)}")
                        reason = "bcma_drug_mismatch"
                        span.set_status(Status(StatusCode.ERROR, f"BCMA fail: {reason}"))
                        span.record_exception(RuntimeError(f"order {order_id}: {reason}"))
                    else:
                        span.set_attribute("bcma.match", True)
                    time.sleep(random.uniform(0.02, 0.06))

            if not do_fail or fail_stage == "occlusion":
                with self._tracer.start_as_current_span("pump_program") as span:
                    span.set_attribute("order.id", order_id)
                    span.set_attribute("pump.id", self.device_id if GROUP == "infusion" else f"pump-{random.randint(1,32):02d}")
                    span.set_attribute("pump.rate_ml_per_hr", rate_ml_hr)
                    span.set_attribute("pump.drug_library_version", int(self._inf_lib_ver))
                    span.set_attribute("pump.dose_within_library", True)
                    time.sleep(random.uniform(0.05, 0.18))

                with self._tracer.start_as_current_span("pump_start") as span:
                    span.set_attribute("order.id", order_id)
                    span.set_attribute("pump.id", self.device_id if GROUP == "infusion" else f"pump-{random.randint(1,32):02d}")
                    span.set_attribute("pump.start_time", time.time())
                    if do_fail and fail_stage == "occlusion":
                        # Pump starts then quickly alarms occlusion.
                        time.sleep(random.uniform(0.02, 0.08))
                    else:
                        time.sleep(random.uniform(0.04, 0.12))

            if do_fail:
                if fail_stage == "occlusion":
                    with self._tracer.start_as_current_span("alarm_occlusion") as span:
                        span.set_attribute("order.id", order_id)
                        span.set_attribute("alarm.type", "downstream_occlusion")
                        span.set_attribute("alarm.pressure_psi", round(random.uniform(15.0, 22.0), 2))
                        span.set_status(Status(StatusCode.ERROR, "occlusion alarm"))
                        time.sleep(random.uniform(0.02, 0.06))
                with self._tracer.start_as_current_span("notify_clinician") as span:
                    span.set_attribute("order.id", order_id)
                    span.set_attribute("notify.channel", random.choice(["pager", "spectralink", "vocera", "epic_secure_chat"]))
                    span.set_attribute("notify.role", random.choice(["rn", "pharmd", "md"]))
                    span.set_status(Status(StatusCode.ERROR, "administration aborted"))
                    time.sleep(random.uniform(0.02, 0.06))
                root.set_status(Status(StatusCode.ERROR, f"administration aborted at {fail_stage}"))
                root.set_attribute("outcome", "aborted")
                root.set_attribute("abort.stage", fail_stage)
            else:
                with self._tracer.start_as_current_span("infusion_complete") as span:
                    span.set_attribute("order.id", order_id)
                    span.set_attribute("infusion.volume_ml", random.randint(25, 500))
                    span.set_attribute("infusion.duration_min", random.randint(15, 240))
                    time.sleep(random.uniform(0.03, 0.08))

                root.set_attribute("outcome", "administered")

    def shutdown(self):
        """Flush and shut down OTel providers."""
        self._tracer_provider.shutdown()
        self._logger_provider.shutdown()
        self._meter_provider.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def trace_loop(simulators: list[DeviceSimulator]):
    """Background thread: emit traces for each asset at ~SCRAPE_INTERVAL_S cadence."""
    time.sleep(random.uniform(5, 15))
    while True:
        for sim in simulators:
            try:
                sim.emit_trace()
            except Exception as exc:
                _root_log.error("trace error asset=%s: %s", sim.device_id, exc)
            time.sleep(random.uniform(0.5, 2.0))
        time.sleep(max(1.0, SCRAPE_INTERVAL_S - len(simulators) * 1.5))


def main():
    profile = PROFILES.get(GROUP)
    if profile is None:
        _root_log.error("Unknown GROUP=%s; valid: %s", GROUP, list(PROFILES.keys()))
        raise SystemExit(1)

    _root_log.info(
        "device-sim starting: group=%s type=%r vendor=%r assets=%d "
        "endpoint=%s interval=%ss failure_rate=%.0f%% intensity=%d",
        GROUP, ASSET_TYPE, ASSET_VENDOR, DEVICE_COUNT,
        OTLP_ENDPOINT, SCRAPE_INTERVAL_S, FAILURE_RATE * 100, INTENSITY,
    )

    # Build one simulator per asset
    simulators: list[DeviceSimulator] = []
    asset_prefix = profile["asset_prefix"]
    for i in range(DEVICE_COUNT):
        device_id = f"{asset_prefix}-{i + 1:02d}"
        pharmacy_sub = None
        ehr_ch = None
        imaging_mod = None
        if GROUP == "pharmacy":
            pharmacy_sub = PHARMACY_SUBTYPES[i % len(PHARMACY_SUBTYPES)]
        if GROUP == "ehr":
            ehr_ch = EHR_CHANNELS[i % len(EHR_CHANNELS)]
        if GROUP == "imaging":
            imaging_mod = IMAGING_MODALITIES[i % len(IMAGING_MODALITIES)]
        sim = DeviceSimulator(device_id=device_id, profile=profile,
                              pharmacy_subtype=pharmacy_sub, ehr_channel=ehr_ch,
                              imaging_modality=imaging_mod)
        simulators.append(sim)
        extra = ""
        if pharmacy_sub is not None:
            extra = f" subtype={pharmacy_sub[0]} setpoint_c={(pharmacy_sub[1]+pharmacy_sub[2])/2.0:.1f}"
        if ehr_ch is not None:
            extra = f" channel={ehr_ch}"
        if imaging_mod is not None:
            extra = f" modality={imaging_mod[0]} field_T={imaging_mod[1]}"
        _root_log.info(
            "  asset registered: %s  type=%s vendor=%s facility=%s unit=%s%s",
            device_id, ASSET_TYPE, ASSET_VENDOR, sim.facility_id, sim.unit, extra,
        )

    # Start trace background thread
    t = threading.Thread(target=trace_loop, args=(simulators,), daemon=True, name="trace-loop")
    t.start()

    # Main loop: tick each asset each interval → updates state + emits logs + metrics.
    elapsed = 0.0
    while True:
        time.sleep(SCRAPE_INTERVAL_S)
        elapsed += SCRAPE_INTERVAL_S
        for sim in simulators:
            try:
                sim.tick(elapsed)
            except Exception as exc:
                _root_log.error("tick error asset=%s: %s", sim.device_id, exc)


if __name__ == "__main__":
    main()
