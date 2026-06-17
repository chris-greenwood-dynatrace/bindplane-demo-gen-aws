"""
device-sim/sim.py
=================
Unified per-device, all-three-signals simulator for the manufacturing demo.

For each machine on the configured line this script builds its own:
    - OTel Resource  (service.namespace, service.name, host.name, machine.*, manufacturing.*)
    - LoggerProvider → OTLP gRPC → edge collector
    - TracerProvider → OTLP gRPC → edge collector
    - MeterProvider  → OTLP gRPC → edge collector

Every SCRAPE_INTERVAL_S seconds:
    - METRICS: machine + production metrics via OTel Metrics SDK over OTLP gRPC.
    - LOGS:    realistic machine events at varied severity levels (INFO/WARN/ERROR)
                         via OTLP gRPC → edge collector.
    - TRACES:  a "production_cycle" parent span with 4 child spans; FAULT_RATE
                         cycles → ERROR, via OTLP gRPC → edge collector.

All three signals carry IDENTICAL identity attributes so Dynatrace correlates
them into a single entity per machine.

Env vars (all optional except OTEL_EXPORTER_OTLP_ENDPOINT):
    LINE                        A | B | C | pkg | util
    LINE_NAME                   Human-readable label (default "Line <LINE>")
    MACHINE_TYPE                e.g. "CNC mill" (default "CNC mill")
    DEVICE_COUNT                machines per line (default 3)
    OTEL_EXPORTER_OTLP_ENDPOINT e.g. line-a:4317   (no http:// prefix; gRPC insecure)
    SCRAPE_INTERVAL_S           seconds between emit cycles (default 30)
    FAULT_RATE                  0.0–1.0 probability of fault per cycle (default 0.08)
    INTENSITY                   1|2|3 – controls metric variety (default 2)
"""

import os
import random
import time
import logging
import sys

# ── OTel SDK imports ──────────────────────────────────────────────────────────
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.trace import StatusCode, SpanKind
from opentelemetry.semconv.resource import ResourceAttributes

# ── Configuration ─────────────────────────────────────────────────────────────
LINE = os.environ.get("LINE", "A")
LINE_NAME = os.environ.get("LINE_NAME", f"Line {LINE}")
MACHINE_TYPE = os.environ.get("MACHINE_TYPE", "CNC mill")
DEVICE_COUNT = int(os.environ.get("DEVICE_COUNT", "3"))
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "localhost:4317")
SCRAPE_INTERVAL_S = int(os.environ.get("SCRAPE_INTERVAL_S", "30"))
FAULT_RATE = float(os.environ.get("FAULT_RATE", "0.08"))
INTENSITY = int(os.environ.get("INTENSITY", "2"))
SITE = "plant-1"

# Normalise OTLP endpoint — strip scheme if provided, gRPC exporter wants host:port
if OTLP_ENDPOINT.startswith("http://"):
    OTLP_ENDPOINT = OTLP_ENDPOINT[len("http://"):]
if OTLP_ENDPOINT.startswith("https://"):
    OTLP_ENDPOINT = OTLP_ENDPOINT[len("https://"):]

GRPC_ENDPOINT = f"http://{OTLP_ENDPOINT}"   # gRPC exporter URL format

# ── Bootstrap root logger ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
root_log = logging.getLogger("device-sim")

# ── Line / machine naming helpers ─────────────────────────────────────────────
LINE_KEY = LINE.lower()

# Sanitise LINE for use as a dimension value (no spaces, commas — use slugified form)
LINE_SLUG = LINE_KEY.replace(" ", "-").replace(",", "")

# Sanitise MACHINE_TYPE for dimension values
def _dim_safe(s: str) -> str:
    """Replace characters not safe in DT line-protocol dimension values."""
    return s.lower().replace(" ", "-").replace("/", "-").replace(",", "").replace("_", "-")

MACHINE_TYPE_SLUG = _dim_safe(MACHINE_TYPE)


def machine_id(idx: int) -> str:
    """Return a stable machine ID like 'line-a-cnc-mill-01'."""
    line_slug = f"line-{LINE_KEY}"
    return f"{line_slug}-{MACHINE_TYPE_SLUG}-{idx:02d}"


# ── Metric sampling helpers ───────────────────────────────────────────────────

def _metric_values_for_machine(
    mid: str,
    parts_completed: int,
    defects: int,
    downtime_s: int,
    is_fault: bool,
    idx: int,
) -> dict:
    """Build one cycle of metric values and deltas for a machine."""
    base_temp = 65.0 + (idx * 2)
    temp = round(base_temp * (1.0 + random.uniform(-0.08, 0.08)), 2)
    if is_fault:
        temp = round(92.0 * (1.0 + random.uniform(-0.05, 0.05)), 2)

    base_vib = 1.5 + (idx * 0.3)
    vib = round(base_vib * (1.0 + random.uniform(-0.15, 0.15)), 3)
    if is_fault:
        vib = round(6.8 * (1.0 + random.uniform(-0.10, 0.10)), 3)

    spindle = round((55.0 + idx * 5) * (1.0 + random.uniform(-0.10, 0.10)), 1)
    power = round((12.0 + idx * 1.5) * (1.0 + random.uniform(-0.07, 0.07)), 2)
    cycle_time = round((45.0 + idx * 3) * (1.0 + random.uniform(-0.12, 0.12)), 1)
    if is_fault:
        cycle_time = round(120.0 * (1.0 + random.uniform(-0.20, 0.20)), 1)

    # OEE gauges
    avail = round(max(0.0, min(1.0, 0.92 - (defects * 0.001))), 4)
    perf = round(max(0.0, min(1.0, 0.88 + random.uniform(-0.03, 0.03))), 4)
    parts_safe = max(1, parts_completed)
    quality = round(max(0.0, min(1.0, 1.0 - (defects / parts_safe))), 4)
    overall = round(avail * perf * quality, 4)

    # For the simulator each tick produces a small delta.
    parts_delta = max(0, random.randint(1, 8))
    defects_delta = max(0, random.randint(1, max(1, parts_delta))) if is_fault else 0
    downtime_delta = max(0, random.randint(30, 300)) if is_fault else 0
    return {
        "machine.temperature": temp,
        "machine.vibration": vib,
        "machine.spindle_load": spindle,
        "machine.power": power,
        "machine.cycle_time": cycle_time,
        "oee.availability": avail,
        "oee.performance": perf,
        "oee.quality": quality,
        "oee.overall": overall,
        "production.parts_completed": parts_delta,
        "production.defects": defects_delta,
        "machine.downtime": downtime_delta,
    }


# ── Per-machine OTel providers ─────────────────────────────────────────────────
class MachineSimulator:
    """Owns OTel log + trace providers for a single simulated machine."""

    def __init__(self, idx: int):
        self.idx = idx
        self.machine_id = machine_id(idx)
        self.resource = self._build_resource()

        # Providers — logs, traces, and metrics all export via OTLP gRPC
        self.tracer_provider = self._build_tracer_provider()
        self.logger_provider = self._build_logger_provider()
        self.meter_provider = self._build_meter_provider()
        self._build_metric_instruments()

        # Python logger wired to OTel log exporter
        self.logger = self._build_logger()

        # Runtime state used by line-protocol builder
        self.parts_completed = 0
        self.defects = 0
        self.downtime_s = 0
        self.cycle_number = 0

        # Fault state from last tick (for log coherence)
        self._last_fault: bool = False

        root_log.info("Initialised simulator for %s → %s", self.machine_id, GRPC_ENDPOINT)

    # ── Resource ──────────────────────────────────────────────────────────────
    def _build_resource(self) -> Resource:
        return Resource.create({
            ResourceAttributes.SERVICE_NAMESPACE: "manufacturing",
            ResourceAttributes.SERVICE_NAME: self.machine_id,
            "host.name": self.machine_id,
            "machine.id": self.machine_id,
            "machine.type": MACHINE_TYPE,
            "manufacturing.line": LINE,
            "manufacturing.site": SITE,
        })

    # ── TracerProvider ────────────────────────────────────────────────────────
    def _build_tracer_provider(self) -> TracerProvider:
        exporter = OTLPSpanExporter(endpoint=GRPC_ENDPOINT, insecure=True)
        provider = TracerProvider(resource=self.resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        return provider

    # ── LoggerProvider ────────────────────────────────────────────────────────
    def _build_logger_provider(self) -> LoggerProvider:
        exporter = OTLPLogExporter(endpoint=GRPC_ENDPOINT, insecure=True)
        provider = LoggerProvider(resource=self.resource)
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        return provider

    # ── MeterProvider ────────────────────────────────────────────────────────
    def _build_meter_provider(self) -> MeterProvider:
        exporter = OTLPMetricExporter(endpoint=GRPC_ENDPOINT, insecure=True)
        reader = PeriodicExportingMetricReader(
            exporter=exporter,
            export_interval_millis=max(1000, SCRAPE_INTERVAL_S * 1000),
        )
        return MeterProvider(resource=self.resource, metric_readers=[reader])

    def _build_metric_instruments(self) -> None:
        meter = self.meter_provider.get_meter("manufacturing.device-sim")

        self._m_temperature = meter.create_histogram("machine.temperature")
        self._m_vibration = meter.create_histogram("machine.vibration")
        self._m_spindle_load = meter.create_histogram("machine.spindle_load")
        self._m_power = meter.create_histogram("machine.power")
        self._m_cycle_time = meter.create_histogram("machine.cycle_time")

        self._m_oee_availability = meter.create_histogram("oee.availability")
        self._m_oee_performance = meter.create_histogram("oee.performance")
        self._m_oee_quality = meter.create_histogram("oee.quality")
        self._m_oee_overall = meter.create_histogram("oee.overall")

        self._c_parts_completed = meter.create_counter("production.parts_completed")
        self._c_defects = meter.create_counter("production.defects")
        self._c_downtime = meter.create_counter("machine.downtime")

    def _metric_attributes(self) -> dict:
        return {
            "machine.id": self.machine_id,
            "machine.type": MACHINE_TYPE,
            "manufacturing.line": LINE,
            "manufacturing.site": SITE,
        }

    # ── Python logger wired into OTel logs ───────────────────────────────────
    def _build_logger(self) -> logging.Logger:
        set_logger_provider(self.logger_provider)
        handler = LoggingHandler(
            level=logging.DEBUG,
            logger_provider=self.logger_provider,
        )
        logger = logging.getLogger(f"machine.{self.machine_id}")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        # Prevent propagation to root so we don't double-emit to stdout
        logger.propagate = False
        # Also attach a stdout handler so operators can see the log stream
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
        ))
        logger.addHandler(stdout_handler)
        return logger

    # ── Logs — machine events ─────────────────────────────────────────────────
    _EVENT_TEMPLATES = [
        # (severity_method, event_type, message_template, fault_only)
        ("info",    "startup",          "Machine {id} startup complete — spindle at {rpm} RPM", False),
        ("info",    "tool_change",      "Machine {id} tool change: slot {slot} replaced (wear index {wear})", False),
        ("info",    "cycle_start",      "Machine {id} starting production cycle {cycle} — job {job}", False),
        ("warning", "maintenance_due",  "Machine {id} maintenance interval approaching — {hours}h since last service", False),
        ("warning", "over_temp",        "Machine {id} temperature {temp}°C exceeds threshold (85°C)", True),
        ("warning", "spindle_overload", "Machine {id} spindle load {load}% — reduce feed rate", True),
        ("error",   "servo_fault",      "Machine {id} SERVO FAULT on axis {axis} — code E{code}", True),
        ("error",   "jam",              "Machine {id} MATERIAL JAM detected — line halted", True),
    ]

    def emit_log(self, is_fault: bool):
        """Emit a realistic machine event log record."""
        candidates = [e for e in self._EVENT_TEMPLATES if not e[3] or is_fault]
        if is_fault:
            fault_events = [e for e in candidates if e[0] == "error" or (e[0] == "warning" and e[3])]
            normal_events = [e for e in candidates if e not in fault_events]
            pool = fault_events * 4 + normal_events
        else:
            pool = candidates
        evt = random.choice(pool)
        severity_fn, event_type, msg_tpl, _ = evt

        msg = msg_tpl.format(
            id=self.machine_id,
            rpm=random.randint(800, 4000),
            slot=random.randint(1, 24),
            wear=round(random.uniform(0.5, 1.0), 2),
            cycle=self.cycle_number,
            job=f"JOB-{random.randint(1000, 9999)}",
            hours=random.randint(200, 800),
            temp=round(random.uniform(86, 100), 1),
            load=round(random.uniform(90, 105), 1),
            axis=random.choice(["X", "Y", "Z", "A", "B"]),
            code=random.randint(1001, 9999),
        )

        extra = {
            "machine.id": self.machine_id,
            "event.type": event_type,
            "manufacturing.line": LINE,
            "manufacturing.site": SITE,
            "machine.type": MACHINE_TYPE,
            "cycle.number": self.cycle_number,
        }
        getattr(self.logger, severity_fn)(msg, extra=extra)

    # ── Traces — production cycle ─────────────────────────────────────────────
    def emit_trace(self, is_fault: bool):
        """Emit a production_cycle parent span with 4 child spans."""
        tracer = self.tracer_provider.get_tracer(f"manufacturing.{self.machine_id}")
        job_id = f"JOB-{random.randint(10000, 99999)}"
        parts_this_cycle = random.randint(1, 8)
        defects_this_cycle = random.randint(1, max(1, parts_this_cycle)) if is_fault else 0

        with tracer.start_as_current_span(
            "production_cycle",
            kind=SpanKind.INTERNAL,
            attributes={
                "job.id": job_id,
                "machine.id": self.machine_id,
                "machine.type": MACHINE_TYPE,
                "manufacturing.line": LINE,
                "manufacturing.site": SITE,
                "parts.planned": parts_this_cycle,
                "parts.defects": defects_this_cycle,
            },
        ) as root_span:
            # load_part
            with tracer.start_as_current_span(
                "load_part",
                kind=SpanKind.INTERNAL,
                attributes={"job.id": job_id, "machine.id": self.machine_id},
            ):
                time.sleep(random.uniform(0.1, 0.3))

            # machining
            with tracer.start_as_current_span(
                "machining",
                kind=SpanKind.INTERNAL,
                attributes={
                    "job.id": job_id,
                    "machine.id": self.machine_id,
                    "spindle.rpm": random.randint(1000, 4000),
                    "feed.rate.mmpm": random.randint(200, 800),
                },
            ) as machining_span:
                time.sleep(random.uniform(0.2, 0.6))
                if is_fault:
                    exc = RuntimeError(f"Fault during machining on {self.machine_id}: servo/jam event")
                    machining_span.record_exception(exc)
                    machining_span.set_status(StatusCode.ERROR, "Machining fault")

            # inspect
            with tracer.start_as_current_span(
                "inspect",
                kind=SpanKind.INTERNAL,
                attributes={
                    "job.id": job_id,
                    "machine.id": self.machine_id,
                    "parts.inspected": parts_this_cycle,
                    "defects.found": defects_this_cycle,
                },
            ):
                time.sleep(random.uniform(0.05, 0.15))

            # unload
            with tracer.start_as_current_span(
                "unload",
                kind=SpanKind.INTERNAL,
                attributes={"job.id": job_id, "machine.id": self.machine_id},
            ):
                time.sleep(random.uniform(0.05, 0.2))

            if is_fault:
                root_span.set_status(StatusCode.ERROR, "Production cycle fault")

        # Update counters
        self.parts_completed += parts_this_cycle
        self.defects += defects_this_cycle
        if is_fault:
            self.downtime_s += random.randint(30, 300)

    # ── One simulation cycle ──────────────────────────────────────────────────
    def tick(self):
        """Execute one simulation interval — emit log + trace + metrics."""
        self.cycle_number += 1
        is_fault = random.random() < FAULT_RATE
        self._last_fault = is_fault
        self.emit_log(is_fault)
        self.emit_trace(is_fault)
        self.emit_metrics(is_fault)

    def emit_metrics(self, is_fault: bool) -> None:
        values = _metric_values_for_machine(
            mid=self.machine_id,
            parts_completed=self.parts_completed,
            defects=self.defects,
            downtime_s=self.downtime_s,
            is_fault=self._last_fault,
            idx=self.idx,
        )

        attrs = self._metric_attributes()
        self._m_temperature.record(values["machine.temperature"], attrs)
        self._m_vibration.record(values["machine.vibration"], attrs)
        self._m_spindle_load.record(values["machine.spindle_load"], attrs)
        self._m_power.record(values["machine.power"], attrs)
        self._m_cycle_time.record(values["machine.cycle_time"], attrs)

        self._m_oee_availability.record(values["oee.availability"], attrs)
        self._m_oee_performance.record(values["oee.performance"], attrs)
        self._m_oee_quality.record(values["oee.quality"], attrs)
        self._m_oee_overall.record(values["oee.overall"], attrs)

        self._c_parts_completed.add(values["production.parts_completed"], attrs)
        self._c_defects.add(values["production.defects"], attrs)
        self._c_downtime.add(values["machine.downtime"], attrs)

    def shutdown(self):
        """Flush and shut down all providers."""
        self.tracer_provider.shutdown()
        self.logger_provider.shutdown()
        self.meter_provider.shutdown()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    root_log.info(
        "device-sim starting | line=%s machine_type=%s device_count=%d "
        "endpoint=%s interval=%ds fault_rate=%.2f intensity=%d",
        LINE, MACHINE_TYPE, DEVICE_COUNT, GRPC_ENDPOINT,
        SCRAPE_INTERVAL_S, FAULT_RATE, INTENSITY,
    )

    # Build one simulator per machine on this line
    machines = [MachineSimulator(i + 1) for i in range(DEVICE_COUNT)]

    root_log.info("All %d machine simulators initialised, entering emit loop", DEVICE_COUNT)

    # Stagger first ticks slightly so all machines don't export simultaneously
    stagger = SCRAPE_INTERVAL_S / max(DEVICE_COUNT, 1)

    try:
        cycle = 0
        while True:
            cycle += 1
            root_log.info("--- Cycle %d (line=%s) ---", cycle, LINE)

            # Tick each machine (emits log + trace)
            for i, machine in enumerate(machines):
                if i > 0:
                    time.sleep(stagger)
                machine.tick()

            # Sleep for the remainder of the interval
            time.sleep(max(1, SCRAPE_INTERVAL_S - int(stagger * DEVICE_COUNT)))
    except KeyboardInterrupt:
        root_log.info("Shutting down device-sim (KeyboardInterrupt)")
    finally:
        for machine in machines:
            machine.shutdown()
        root_log.info("device-sim shutdown complete")


if __name__ == "__main__":
    main()
