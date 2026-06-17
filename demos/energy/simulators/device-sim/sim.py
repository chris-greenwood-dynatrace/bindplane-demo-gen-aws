"""device-sim — unified per-asset-group simulator emitting logs + traces + metrics via OTLP.

One image, six compose services (one per asset group: substation, transformer, feeder,
meter, gen, scada). Each container simulates DEVICE_COUNT assets in its group. For EACH
asset, a dedicated OTel Resource + LoggerProvider + TracerProvider is built so logs and
traces carry IDENTICAL resource attributes, enabling Dynatrace to correlate them into a
single entity per asset.

Metrics are emitted with the OpenTelemetry Metrics SDK over OTLP gRPC to the same edge
collector endpoint used by logs and traces.

Env vars:
  GROUP                     asset group (substation|transformer|feeder|meter|gen|scada)
  GROUP_NAME                human-readable group label
  ASSET_TYPE                asset type string (e.g. "hv substation")
  ASSET_VENDOR              vendor string (e.g. "SEL")
  DEVICE_COUNT              number of assets to simulate per container (default 3)
  OTEL_EXPORTER_OTLP_ENDPOINT   gRPC endpoint for the edge collector (e.g. substations:4317)
  SCRAPE_INTERVAL_S         metric publish interval in seconds (default 30)
  FAILURE_RATE              fault injection rate 0.0-1.0 (default 0.08)
  INTENSITY                 1=minimal 2=standard 3=high (default 2)
Signal contract:
  METRICS  — OTel metrics over OTLP gRPC to edge collector.
  LOGS     — Utility-style structured log records via OTel Logs SDK → edge collector.
  TRACES   — "grid_operation" spans (fault_isolation | load_shed | restoration | dispatch)
             → edge collector.
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
GROUP = os.environ.get("GROUP", "substation")
GROUP_NAME = os.environ.get("GROUP_NAME", GROUP)
ASSET_TYPE = os.environ.get("ASSET_TYPE", "hv substation")
ASSET_VENDOR = os.environ.get("ASSET_VENDOR", "SEL")
DEVICE_COUNT = int(os.environ.get("DEVICE_COUNT", "3"))
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "substations:4317")
SCRAPE_INTERVAL_S = float(os.environ.get("SCRAPE_INTERVAL_S", "30"))
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0.08"))
INTENSITY = int(os.environ.get("INTENSITY", "2"))
REGION = "region-east-1"

# ── Asset profiles ───────────────────────────────────────────────────────────
# Each profile selects which metric families to emit and which logs to weight up.
# Per-asset numerical baselines are randomized in DeviceSimulator.__init__.
PROFILES = {
    "substation": {
        "asset_prefix": "sub",
        "voltage_class_kv": [115.0, 138.0, 230.0, 345.0],
        "emit": {"substation": True},
        "log_set": "substation",
    },
    "transformer": {
        "asset_prefix": "xfmr",
        "voltage_class_kv": [13.8, 25.0, 34.5, 69.0],
        "emit": {"transformer": True},
        "log_set": "transformer",
    },
    "feeder": {
        "asset_prefix": "fdr",
        "voltage_class_kv": [13.8, 25.0],
        "emit": {"feeder": True},
        "log_set": "feeder",
    },
    "meter": {
        "asset_prefix": "ami",
        "voltage_class_kv": [0.24],
        "emit": {"meter": True},
        "log_set": "meter",
    },
    "gen": {
        "asset_prefix": "gen",
        "voltage_class_kv": [13.8, 25.0, 34.5],
        "emit": {"gen": True},
        "log_set": "gen",
    },
    "scada": {
        "asset_prefix": "rtu",
        "voltage_class_kv": [0.0],
        "emit": {"scada": True},
        "log_set": "scada",
    },
}

# ── Generation sub-types (rotated when GROUP=gen) ────────────────────────────
GEN_SUBTYPES = ["solar_pv", "wind_turbine", "battery_storage", "gas_peaker"]


# ── Log message templates per asset group ───────────────────────────────────
def _ip():
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def _bus():
    return f"BUS-{random.randint(1, 12):02d}"


def _zone():
    return f"Z{random.randint(1, 4)}"


def _meter_id():
    return f"M{random.randint(100000, 999999)}"


# Each tuple: (severity_name, log_level, event_type, message_fn).
_SUBSTATION_LOGS = [
    ("INFO",     logging.INFO,    "breaker_close",
     lambda: f"BREAKER-CLOSE: bus={_bus()} cb={random.randint(1,12)} state=CLOSED operator=auto"),
    ("INFO",     logging.INFO,    "synch_check_ok",
     lambda: f"SYNCH-CHECK: bus={_bus()} dV={random.uniform(-0.5,0.5):.2f}% df={random.uniform(-0.05,0.05):.3f}Hz status=OK"),
    ("WARNING",  logging.WARNING, "breaker_open",
     lambda: f"BREAKER-OPEN: bus={_bus()} cb={random.randint(1,12)} reason=protection-trip"),
    ("WARNING",  logging.WARNING, "goose_late",
     lambda: f"GOOSE-LATE: zone={_zone()} dt={random.randint(20,80)}ms threshold=10ms"),
    ("ERROR",    logging.ERROR,   "relay_trip_overcurrent",
     lambda: f"SEL-RELAY-TRIP: zone={_zone()} element=51P I={random.uniform(2.0,5.0):.2f}xCT operate_t={random.uniform(0.05,0.4):.3f}s"),
    ("ERROR",    logging.ERROR,   "relay_trip_distance",
     lambda: f"SEL-RELAY-TRIP: zone={_zone()} element=21P reach={random.uniform(0.3,1.2):.2f}pu fault_loc=line"),
    ("CRITICAL", logging.CRITICAL, "goose_timeout",
     lambda: f"GOOSE-TIMEOUT: peer={_ip()} dataset=PROT/Z1 missed=3"),
]

_TRANSFORMER_LOGS = [
    ("INFO",     logging.INFO,    "ltc_tap_lower",
     lambda: f"LTC-TAP-LOWER: tap={random.randint(-8,0)} secondary_kv={random.uniform(12.5,13.5):.3f}"),
    ("INFO",     logging.INFO,    "ltc_tap_raise",
     lambda: f"LTC-TAP-RAISE: tap={random.randint(0,8)} secondary_kv={random.uniform(13.5,14.5):.3f}"),
    ("INFO",     logging.INFO,    "cooling_fan_start",
     lambda: f"COOLING-FAN-START: bank={random.randint(1,3)} ambient_c={random.uniform(28,40):.1f}"),
    ("WARNING",  logging.WARNING, "oil_temp_high",
     lambda: f"OIL-TEMP-HIGH: top_oil={random.uniform(85,98):.1f}C threshold=85C"),
    ("WARNING",  logging.WARNING, "winding_temp_high",
     lambda: f"WINDING-TEMP-HIGH: hot_spot={random.uniform(105,118):.1f}C threshold=105C"),
    ("WARNING",  logging.WARNING, "load_high",
     lambda: f"LOAD-HIGH: load_pct={random.uniform(95,118):.1f}% nameplate=ANSI"),
    ("ERROR",    logging.ERROR,   "gas_in_oil_alarm",
     lambda: f"DGA-ALARM: H2={random.randint(150,400)}ppm CH4={random.randint(80,250)}ppm severity=ALERT"),
    ("ERROR",    logging.ERROR,   "oil_level_low",
     lambda: f"OIL-LEVEL-LOW: level_pct={random.uniform(15,30):.1f}% threshold=35%"),
]

_FEEDER_LOGS = [
    ("INFO",     logging.INFO,    "capbank_close",
     lambda: f"CAPBANK-SWITCH: bank={random.randint(1,4)} state=CLOSED kvar={random.choice([300,600,900,1200])}"),
    ("INFO",     logging.INFO,    "capbank_open",
     lambda: f"CAPBANK-SWITCH: bank={random.randint(1,4)} state=OPEN kvar={random.choice([300,600,900,1200])}"),
    ("INFO",     logging.INFO,    "voltage_normal",
     lambda: f"VOLTAGE-NORMAL: feeder={random.randint(1,8)} v_pu={random.uniform(0.985,1.015):.4f}"),
    ("WARNING",  logging.WARNING, "voltage_sag",
     lambda: f"VOLTAGE-SAG: feeder={random.randint(1,8)} v_pu={random.uniform(0.85,0.94):.4f} duration_ms={random.randint(80,500)}"),
    ("WARNING",  logging.WARNING, "momentary_fault",
     lambda: f"MOMENTARY-FAULT: feeder={random.randint(1,8)} type={random.choice(['SLG','LL','LLG'])} cleared=true"),
    ("WARNING",  logging.WARNING, "recloser_op",
     lambda: f"RECLOSER-OPERATION: device=R{random.randint(1,8)} shot={random.randint(1,3)} result=reclose-success"),
    ("ERROR",    logging.ERROR,   "recloser_lockout",
     lambda: f"RECLOSER-LOCKOUT: device=R{random.randint(1,8)} shots=3 fault_amps={random.randint(800,2200)}A"),
    ("ERROR",    logging.ERROR,   "voltage_swell",
     lambda: f"VOLTAGE-SWELL: feeder={random.randint(1,8)} v_pu={random.uniform(1.10,1.18):.4f} duration_ms={random.randint(120,700)}"),
]

_METER_LOGS = [
    ("INFO",     logging.INFO,    "ami_read_ok",
     lambda: f"AMI-READ-OK: meter={_meter_id()} kwh={random.uniform(1000,55000):.2f} window=15m"),
    ("INFO",     logging.INFO,    "meter_restoration",
     lambda: f"METER-RESTORATION: meter={_meter_id()} outage_duration_s={random.randint(30,1800)}"),
    ("INFO",     logging.INFO,    "power_quality_normal",
     lambda: f"POWER-QUALITY: meter={_meter_id()} thd_pct={random.uniform(0.8,2.5):.2f} v_pu={random.uniform(0.99,1.01):.4f}"),
    ("WARNING",  logging.WARNING, "ami_comm_fail",
     lambda: f"AMI-COMM-FAIL: meter={_meter_id()} retries=3 last_seen_s={random.randint(60,1800)}"),
    ("WARNING",  logging.WARNING, "meter_outage_notification",
     lambda: f"METER-OUTAGE-NOTIFICATION: meter={_meter_id()} count_15m={random.randint(1,4)}"),
    ("WARNING",  logging.WARNING, "tamper_detected",
     lambda: f"TAMPER-DETECTED: meter={_meter_id()} type={random.choice(['cover','reverse_flow','magnetic'])}"),
    ("ERROR",    logging.ERROR,   "outage_cluster",
     lambda: f"OUTAGE-CLUSTER: feeder={random.randint(1,8)} affected_meters={random.randint(50,800)} duration_s={random.randint(120,3600)}"),
]

_GEN_LOGS = [
    ("INFO",     logging.INFO,    "generator_start",
     lambda: f"GENERATOR-START: unit={random.randint(1,4)} ramp_mw_per_min={random.uniform(2.0,8.0):.1f}"),
    ("INFO",     logging.INFO,    "battery_charge_cycle",
     lambda: f"BATTERY-CYCLE: unit={random.randint(1,4)} mode=charge soc_pct={random.uniform(20,80):.1f}"),
    ("INFO",     logging.INFO,    "battery_discharge_cycle",
     lambda: f"BATTERY-CYCLE: unit={random.randint(1,4)} mode=discharge soc_pct={random.uniform(20,80):.1f}"),
    ("INFO",     logging.INFO,    "ssr_command",
     lambda: f"AGC-SETPOINT: unit={random.randint(1,4)} setpoint_mw={random.uniform(5,80):.2f}"),
    ("WARNING",  logging.WARNING, "curtailment",
     lambda: f"GEN-CURTAILMENT: unit={random.randint(1,4)} reason={random.choice(['oversupply','transmission','frequency'])} mw_curtailed={random.uniform(2,40):.2f}"),
    ("WARNING",  logging.WARNING, "inverter_warning",
     lambda: f"INVERTER-WARN: unit={random.randint(1,4)} code={random.choice(['DC_HIGH','TEMP_HIGH','GROUND_FAULT'])}"),
    ("ERROR",    logging.ERROR,   "inverter_fault",
     lambda: f"INVERTER-FAULT: unit={random.randint(1,4)} code=F{random.randint(100,499)} action=trip"),
    ("ERROR",    logging.ERROR,   "generator_trip",
     lambda: f"GENERATOR-TRIP: unit={random.randint(1,4)} reason={random.choice(['overspeed','reverse_power','loss_of_field'])}"),
]

_SCADA_LOGS = [
    ("INFO",     logging.INFO,    "rtu_heartbeat_ok",
     lambda: f"DNP3-HEARTBEAT: rtu={_ip()} class0_poll_ms={random.randint(80,250)}"),
    ("INFO",     logging.INFO,    "dnp3_unsol",
     lambda: f"DNP3-UNSOL: rtu={_ip()} class={random.choice([1,2,3])} points={random.randint(1,12)}"),
    ("INFO",     logging.INFO,    "command_success",
     lambda: f"COMMAND-SUCCESS: rtu={_ip()} point={random.randint(1,256)} op={random.choice(['CLOSE','OPEN','RAISE','LOWER'])}"),
    ("WARNING",  logging.WARNING, "rtu_slow_poll",
     lambda: f"DNP3-SLOW: rtu={_ip()} class0_poll_ms={random.randint(800,2500)} threshold=500ms"),
    ("WARNING",  logging.WARNING, "comm_link_degraded",
     lambda: f"COMM-LINK-DEGRADED: rtu={_ip()} loss_pct={random.uniform(2,8):.2f} retries={random.randint(2,6)}"),
    ("ERROR",    logging.ERROR,   "rtu_heartbeat_lost",
     lambda: f"DNP3-HEARTBEAT-LOST: rtu={_ip()} last_seen_s={random.randint(60,600)}"),
    ("ERROR",    logging.ERROR,   "command_timeout",
     lambda: f"COMMAND-TIMEOUT: rtu={_ip()} point={random.randint(1,256)} timeout_s={random.randint(5,30)}"),
    ("CRITICAL", logging.CRITICAL, "rtu_offline",
     lambda: f"RTU-OFFLINE: rtu={_ip()} duration_s={random.randint(120,3600)} comm_path={random.choice(['cellular','radio','fiber'])}"),
]

LOG_SETS = {
    "substation": _SUBSTATION_LOGS,
    "transformer": _TRANSFORMER_LOGS,
    "feeder": _FEEDER_LOGS,
    "meter": _METER_LOGS,
    "gen": _GEN_LOGS,
    "scada": _SCADA_LOGS,
}

# Default severity weights match the order of templates in each set above.
LOG_WEIGHTS = {
    "substation": [50, 40, 25, 12, 8, 6, 3],
    "transformer": [40, 40, 35, 18, 12, 10, 5, 4],
    "feeder": [30, 30, 50, 18, 15, 25, 4, 3],
    "meter": [70, 30, 40, 20, 15, 4, 2],
    "gen": [25, 30, 30, 35, 18, 15, 6, 4],
    "scada": [80, 40, 60, 18, 14, 6, 4, 1],
}

# ── Trace operation types ────────────────────────────────────────────────────
OPERATION_TYPES = ["fault_isolation", "load_shed", "restoration", "dispatch"]
ACTION_DETAILS = {
    "fault_isolation": ["sectionalize", "transfer_load", "isolate_zone", "open_recloser"],
    "load_shed":       ["curtail_dr", "interruptible_load", "rotating_outage", "voltage_reduction"],
    "restoration":     ["energize_feeder", "close_tie", "ladder_restoration", "manual_pickup"],
    "dispatch":        ["agc_setpoint", "regulating_reserve", "spinning_reserve", "frequency_response"],
}


def _dim_safe(s: str) -> str:
    """Sanitise a string for use as a metric attribute value."""
    return s.lower().replace(" ", "-").replace("/", "-").replace(",", "").replace("_", "-").replace("&", "and")


# ─────────────────────────────────────────────────────────────────────────────
# Per-asset simulator class
# ─────────────────────────────────────────────────────────────────────────────

class DeviceSimulator:
    """Holds OTel log + trace + meter providers for one simulated grid asset."""

    def __init__(self, device_id: str, profile: dict, gen_subtype: str | None = None):
        self.device_id = device_id
        self.profile = profile
        self.gen_subtype = gen_subtype
        self.voltage_class_kv = random.choice(profile["voltage_class_kv"])

        # Shared resource attributes — IDENTICAL across logs, traces, and metrics.
        attrs = {
            "service.namespace": "energy",
            "service.name": device_id,
            "host.name": device_id,
            "asset.id": device_id,
            "asset.type": ASSET_TYPE,
            "asset.vendor": ASSET_VENDOR,
            "energy.assetgroup": GROUP,
            "energy.region": REGION,
            "energy.voltage_class_kv": self.voltage_class_kv,
        }
        if gen_subtype is not None:
            attrs["energy.gen.subtype"] = gen_subtype
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

        # ── Substation instruments ────────────────────────────────────────
        self._m_voltage_kv      = self._meter.create_histogram("energy.substation.voltage_kv")
        self._m_current_amps    = self._meter.create_histogram("energy.substation.current_amps")
        self._m_frequency_hz    = self._meter.create_histogram("energy.substation.frequency_hz")
        self._m_breaker_status  = self._meter.create_histogram("energy.substation.breaker_status")
        self._c_relay_trips     = self._meter.create_counter("energy.substation.relay_trip_count")

        # ── Transformer instruments ───────────────────────────────────────
        self._m_oil_temp        = self._meter.create_histogram("energy.transformer.oil_temp_c")
        self._m_winding_temp    = self._meter.create_histogram("energy.transformer.winding_temp_c")
        self._m_load_pct        = self._meter.create_histogram("energy.transformer.load_pct")
        self._m_ltc_tap         = self._meter.create_histogram("energy.transformer.ltc_tap_position")
        self._m_oil_level       = self._meter.create_histogram("energy.transformer.oil_level_pct")

        # ── Feeder instruments ────────────────────────────────────────────
        self._m_feeder_load     = self._meter.create_histogram("energy.feeder.load_amps")
        self._m_voltage_dev     = self._meter.create_histogram("energy.feeder.voltage_deviation_pct")
        self._c_recloser_ops    = self._meter.create_counter("energy.feeder.recloser_operations")
        self._c_momentary_outs  = self._meter.create_counter("energy.feeder.momentary_outages")
        self._m_capbank_kvar    = self._meter.create_histogram("energy.feeder.capbank_kvar")

        # ── Meter (AMI) instruments ───────────────────────────────────────
        self._m_meters_online   = self._meter.create_histogram("energy.meter.online_count")
        self._m_meters_outage   = self._meter.create_histogram("energy.meter.outage_count")
        self._m_ami_read_rate   = self._meter.create_histogram("energy.meter.ami_read_success_rate")
        self._m_peak_demand_kw  = self._meter.create_histogram("energy.meter.peak_demand_kw")

        # ── Generation instruments ────────────────────────────────────────
        self._m_gen_output_mw   = self._meter.create_histogram("energy.gen.output_mw")
        self._m_gen_avail_pct   = self._meter.create_histogram("energy.gen.availability_pct")
        self._m_battery_soc     = self._meter.create_histogram("energy.battery.soc_pct")
        self._m_solar_irrad     = self._meter.create_histogram("energy.solar.irradiance_wm2")
        self._m_wind_speed      = self._meter.create_histogram("energy.wind.speed_mps")

        # ── SCADA instruments ─────────────────────────────────────────────
        self._m_rtu_online      = self._meter.create_histogram("energy.scada.rtu_online")
        self._m_scada_poll_lat  = self._meter.create_histogram("energy.scada.poll_latency_ms")
        self._m_command_rate    = self._meter.create_histogram("energy.scada.command_success_rate")
        self._m_comm_link       = self._meter.create_histogram("energy.scada.comm_link_status")

        # ── Per-asset simulated state baselines ───────────────────────────
        self._cycle_offset = random.uniform(0, 2 * math.pi)
        # substation
        self._current_amps = random.uniform(400.0, 1200.0)
        self._frequency_hz = 60.0
        self._breaker_status = 1  # 1=closed/normal, 0=open
        self._relay_trip_total = 0
        # transformer
        self._oil_temp = random.uniform(55.0, 70.0)
        self._winding_temp = random.uniform(70.0, 85.0)
        self._load_pct = random.uniform(50.0, 75.0)
        self._ltc_tap = random.randint(-4, 4)
        self._oil_level_pct = random.uniform(80.0, 95.0)
        # feeder
        self._feeder_load_amps = random.uniform(150.0, 400.0)
        self._voltage_dev_pct = 0.0
        self._recloser_ops_total = 0
        self._momentary_outs_total = 0
        self._capbank_kvar = random.choice([0, 300, 600, 900])
        # meter (AMI concentrator state — represents a fleet under one collector)
        self._meters_under_concentrator = random.randint(800, 2500)
        self._meters_online = self._meters_under_concentrator
        self._meters_outage = 0
        self._ami_read_rate = random.uniform(0.985, 0.998)
        self._peak_demand_kw = random.uniform(1500.0, 5500.0)
        # gen
        self._gen_capacity_mw = random.uniform(20.0, 120.0)
        self._gen_output_mw = self._gen_capacity_mw * random.uniform(0.4, 0.8)
        self._gen_avail_pct = random.uniform(95.0, 100.0)
        self._battery_soc_pct = random.uniform(35.0, 75.0)
        self._solar_irradiance = 600.0
        self._wind_speed_mps = random.uniform(4.0, 9.0)
        # scada
        self._rtu_online_state = 1
        self._scada_poll_latency_ms = random.uniform(80.0, 220.0)
        self._command_success_rate = random.uniform(0.985, 0.999)
        self._comm_link_state = 1

        self._prev_relay_trips = 0
        self._prev_recloser_ops = 0
        self._prev_momentary_outs = 0

    # ── Simulation tick ───────────────────────────────────────────────────
    def tick(self, t: float):
        """Update simulated values for the current time step t (monotonic seconds)."""
        phase = t / 300.0 + self._cycle_offset
        fault = random.random() < FAILURE_RATE

        # Frequency: tightly regulated around 60 Hz with small wander, large excursion on fault.
        self._frequency_hz = max(58.0, min(62.0,
            60.0 + 0.02 * math.sin(phase * 2.5) + random.gauss(0, 0.01)
            + (random.uniform(-0.6, 0.6) if fault else 0.0)
        ))

        # Substation current: load curve + noise, drop on breaker open
        self._current_amps = max(0.0,
            self._current_amps + 6.0 * math.sin(phase) + random.gauss(0, 12.0)
        )
        self._current_amps = max(50.0, min(2400.0, self._current_amps))
        if fault and GROUP == "substation" and random.random() < 0.5:
            self._breaker_status = 0
            self._relay_trip_total += 1
            self._current_amps *= random.uniform(0.05, 0.2)
        else:
            # auto-reclose simulated by gradual return
            self._breaker_status = 1

        # Transformer thermal model
        target_load = 60.0 + 12.0 * math.sin(phase * 0.8) + (random.uniform(20, 35) if fault else 0.0)
        self._load_pct = max(0.0, min(140.0, 0.7 * self._load_pct + 0.3 * target_load))
        amb = 30.0 + 6.0 * math.sin(phase * 0.5)
        self._oil_temp = max(20.0, min(120.0,
            amb + 0.45 * self._load_pct + random.gauss(0, 1.2)
        ))
        self._winding_temp = max(self._oil_temp,
            self._oil_temp + 12.0 + 0.18 * self._load_pct + random.gauss(0, 1.5)
        )
        self._oil_level_pct = max(0.0, min(100.0,
            self._oil_level_pct - (random.uniform(0.05, 0.20) if fault else 0.0)
        ))
        if fault and GROUP == "transformer" and random.random() < 0.25:
            # tap movement
            self._ltc_tap = max(-12, min(12, self._ltc_tap + random.choice([-1, 1])))

        # Feeder
        self._feeder_load_amps = max(0.0,
            300.0 + 60.0 * math.sin(phase) + random.gauss(0, 18.0)
            + (random.uniform(150, 350) if fault else 0.0)
        )
        self._voltage_dev_pct = (random.gauss(0.0, 0.3)
                                 + (random.uniform(-6.0, -2.0) if fault else 0.0))
        if fault and GROUP == "feeder":
            if random.random() < 0.4:
                self._recloser_ops_total += random.randint(1, 3)
            if random.random() < 0.5:
                self._momentary_outs_total += 1
        self._capbank_kvar = random.choice([0, 0, 300, 600, 900, 1200])

        # AMI fleet — outage cluster on fault
        normal_outage = int(self._meters_under_concentrator * random.uniform(0.0001, 0.001))
        fault_outage = int(self._meters_under_concentrator * random.uniform(0.05, 0.20)) if fault else 0
        self._meters_outage = normal_outage + fault_outage
        self._meters_online = max(0, self._meters_under_concentrator - self._meters_outage)
        self._ami_read_rate = max(0.5, min(1.0,
            self._ami_read_rate + random.gauss(0, 0.0005)
            - (random.uniform(0.01, 0.05) if fault else 0.0)
        ))
        self._peak_demand_kw = max(0.0,
            3000.0 + 1200.0 * math.sin(phase * 0.6) + random.gauss(0, 80.0)
            + (random.uniform(-800, 1500) if fault else 0.0)
        )

        # Generation — solar/wind diurnal + curtailment events
        self._solar_irradiance = max(0.0,
            550.0 + 350.0 * math.sin(phase * 0.4) + random.gauss(0, 40.0)
            - (random.uniform(80, 200) if fault else 0.0)
        )
        self._wind_speed_mps = max(0.0,
            7.0 + 3.0 * math.sin(phase * 0.7) + random.gauss(0, 0.6)
            + (random.uniform(-2.0, 2.0) if fault else 0.0)
        )
        target_output = self._gen_capacity_mw * random.uniform(0.55, 0.85)
        if fault and GROUP == "gen":
            target_output *= random.uniform(0.2, 0.6)  # curtailment
        self._gen_output_mw = max(0.0, 0.7 * self._gen_output_mw + 0.3 * target_output)
        self._gen_avail_pct = max(0.0, min(100.0,
            self._gen_avail_pct + random.gauss(0, 0.1)
            - (random.uniform(2.0, 8.0) if fault else 0.0)
        ))
        # battery cycles
        delta_soc = math.sin(phase * 0.9) * 1.5 + random.gauss(0, 0.4)
        self._battery_soc_pct = max(5.0, min(95.0, self._battery_soc_pct + delta_soc))

        # SCADA
        self._scada_poll_latency_ms = max(20.0,
            150.0 + 40.0 * math.sin(phase * 1.3) + random.gauss(0, 25.0)
            + (random.uniform(400, 1500) if fault else 0.0)
        )
        self._command_success_rate = max(0.5, min(1.0,
            self._command_success_rate + random.gauss(0, 0.0008)
            - (random.uniform(0.02, 0.08) if fault else 0.0)
        ))
        self._rtu_online_state = 0 if (fault and GROUP == "scada" and random.random() < 0.4) else 1
        self._comm_link_state = 0 if (fault and random.random() < 0.15) else 1

        _root_log.info(
            "group=%s asset=%s freq=%.3fHz load=%.1f%% gen=%.2fMW soc=%.1f%% fault=%s",
            GROUP, self.device_id, self._frequency_hz, self._load_pct,
            self._gen_output_mw, self._battery_soc_pct, fault,
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
            "energy.assetgroup": GROUP,
            "energy.region": REGION,
            "energy.voltage_class_kv": str(self.voltage_class_kv),
        }
        if self.gen_subtype is not None:
            asset_attrs["energy.gen.subtype"] = self.gen_subtype

        emit = self.profile["emit"]

        if emit.get("substation"):
            self._m_voltage_kv.record(round(self.voltage_class_kv, 3), asset_attrs)
            self._m_current_amps.record(round(self._current_amps, 2), asset_attrs)
            self._m_frequency_hz.record(round(self._frequency_hz, 4), asset_attrs)
            self._m_breaker_status.record(self._breaker_status, asset_attrs)
            delta_trips = max(0, self._relay_trip_total - self._prev_relay_trips)
            self._c_relay_trips.add(delta_trips, asset_attrs)
            self._prev_relay_trips = self._relay_trip_total

        if emit.get("transformer"):
            self._m_oil_temp.record(round(self._oil_temp, 2), asset_attrs)
            self._m_winding_temp.record(round(self._winding_temp, 2), asset_attrs)
            self._m_load_pct.record(round(self._load_pct, 2), asset_attrs)
            self._m_ltc_tap.record(self._ltc_tap, asset_attrs)
            self._m_oil_level.record(round(self._oil_level_pct, 2), asset_attrs)

        if emit.get("feeder"):
            self._m_feeder_load.record(round(self._feeder_load_amps, 2), asset_attrs)
            self._m_voltage_dev.record(round(self._voltage_dev_pct, 4), asset_attrs)
            delta_recl = max(0, self._recloser_ops_total - self._prev_recloser_ops)
            delta_mom = max(0, self._momentary_outs_total - self._prev_momentary_outs)
            self._c_recloser_ops.add(delta_recl, asset_attrs)
            self._c_momentary_outs.add(delta_mom, asset_attrs)
            self._prev_recloser_ops = self._recloser_ops_total
            self._prev_momentary_outs = self._momentary_outs_total
            self._m_capbank_kvar.record(self._capbank_kvar, asset_attrs)

        if emit.get("meter"):
            self._m_meters_online.record(self._meters_online, asset_attrs)
            self._m_meters_outage.record(self._meters_outage, asset_attrs)
            self._m_ami_read_rate.record(round(self._ami_read_rate, 5), asset_attrs)
            self._m_peak_demand_kw.record(round(self._peak_demand_kw, 2), asset_attrs)

        if emit.get("gen"):
            self._m_gen_output_mw.record(round(self._gen_output_mw, 3), asset_attrs)
            self._m_gen_avail_pct.record(round(self._gen_avail_pct, 2), asset_attrs)
            self._m_battery_soc.record(round(self._battery_soc_pct, 2), asset_attrs)
            self._m_solar_irrad.record(round(self._solar_irradiance, 2), asset_attrs)
            self._m_wind_speed.record(round(self._wind_speed_mps, 2), asset_attrs)

        if emit.get("scada"):
            self._m_rtu_online.record(self._rtu_online_state, asset_attrs)
            self._m_scada_poll_lat.record(round(self._scada_poll_latency_ms, 2), asset_attrs)
            self._m_command_rate.record(round(self._command_success_rate, 5), asset_attrs)
            self._m_comm_link.record(self._comm_link_state, asset_attrs)

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
        """Emit one grid_operation trace with child spans."""
        op_type = random.choice(OPERATION_TYPES)
        action_detail = random.choice(ACTION_DETAILS[op_type])
        change_id = f"GRID-{uuid.uuid4().hex[:8].upper()}"
        feeder_no = random.randint(1, 24)
        zone = f"Z{random.randint(1, 4)}"
        do_fail = random.random() < FAILURE_RATE

        with self._tracer.start_as_current_span("grid_operation") as root:
            root.set_attribute("change.id", change_id)
            root.set_attribute("operation.type", op_type)
            root.set_attribute("asset.id", self.device_id)
            root.set_attribute("energy.assetgroup", GROUP)
            root.set_attribute("energy.region", REGION)
            root.set_attribute("energy.voltage_class_kv", self.voltage_class_kv)
            root.set_attribute("feeder", feeder_no)
            root.set_attribute("zone", zone)
            root.set_attribute("action.detail", action_detail)
            time.sleep(random.uniform(0.005, 0.02))

            with self._tracer.start_as_current_span("detect") as span:
                span.set_attribute("change.id", change_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("detect.signals", random.randint(2, 8))
                time.sleep(random.uniform(0.01, 0.05))

            with self._tracer.start_as_current_span("isolate") as span:
                span.set_attribute("change.id", change_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("isolate.element", action_detail)
                span.set_attribute("zone", zone)
                time.sleep(random.uniform(0.01, 0.04))

            with self._tracer.start_as_current_span("dispatch") as span:
                span.set_attribute("change.id", change_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("dispatch.target", random.choice(["agc", "der", "battery", "field_crew"]))
                span.set_attribute("setpoint_mw", round(random.uniform(2.0, 50.0), 2))
                if do_fail:
                    span.set_status(Status(StatusCode.ERROR, "dispatch rejected by EMS"))
                    span.record_exception(
                        RuntimeError(f"Asset {self.device_id}: dispatch conflict on {action_detail}")
                    )
                time.sleep(random.uniform(0.05, 0.15))

            if do_fail:
                with self._tracer.start_as_current_span("rollback") as span:
                    span.set_attribute("change.id", change_id)
                    span.set_attribute("asset.id", self.device_id)
                    span.set_attribute("reason", "dispatch-rejected")
                    span.set_status(Status(StatusCode.ERROR, "rollback after dispatch failure"))
                    time.sleep(random.uniform(0.03, 0.10))
                root.set_status(Status(StatusCode.ERROR, "operation failed — rolled back"))
                root.set_attribute("outcome", "rollback")
            else:
                with self._tracer.start_as_current_span("verify") as span:
                    span.set_attribute("change.id", change_id)
                    span.set_attribute("asset.id", self.device_id)
                    span.set_attribute("verify.checks", random.randint(2, 5))
                    time.sleep(random.uniform(0.02, 0.08))
                root.set_attribute("outcome", "success")

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
        gen_subtype = None
        if GROUP == "gen":
            gen_subtype = GEN_SUBTYPES[i % len(GEN_SUBTYPES)]
        sim = DeviceSimulator(device_id=device_id, profile=profile, gen_subtype=gen_subtype)
        simulators.append(sim)
        extra = f" subtype={gen_subtype}" if gen_subtype else ""
        _root_log.info(
            "  asset registered: %s  type=%s vendor=%s%s",
            device_id, ASSET_TYPE, ASSET_VENDOR, extra,
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
