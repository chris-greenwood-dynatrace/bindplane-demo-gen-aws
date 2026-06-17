"""device-sim — unified per-device-group simulator emitting logs + traces + metrics via OTLP.

One image, four compose services (one per device group). Each container simulates
DEVICE_COUNT devices in its group. For EACH device, a dedicated OTel Resource +
LoggerProvider + TracerProvider is built so logs and traces carry IDENTICAL resource
attributes, enabling Dynatrace to correlate them into a single entity per device.

Metrics are emitted with the OpenTelemetry Metrics SDK over OTLP gRPC to the
same edge collector endpoint used by logs and traces.

Env vars:
  GROUP                     device group (core|edge|fw|lb)
  GROUP_NAME                human-readable group label
  DEVICE_TYPE               device type string (e.g. "core router")
  DEVICE_VENDOR             vendor string (e.g. "Cisco")
  DEVICE_COUNT              number of devices to simulate per container (default 3)
  INTERFACES_PER_DEVICE     interfaces per device (default 4)
  OTEL_EXPORTER_OTLP_ENDPOINT   gRPC endpoint for the edge collector (e.g. site-core:4317)
  SCRAPE_INTERVAL_S         metric publish interval in seconds (default 30)
  FAILURE_RATE              fault injection rate 0.0-1.0 (default 0.08)
  INTENSITY                 1=minimal 2=standard 3=high (default 2)
Signal contract:
    METRICS  — OTel metrics over OTLP gRPC to edge collector.
  LOGS     — Cisco/Juniper-style structured log records via OTel Logs SDK → edge collector.
  TRACES   — "network_operation" spans (config_change | route_update) → edge collector.
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
import threading
import uuid

# ── OpenTelemetry imports (logs + traces only; no MeterProvider) ──────────────
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

# ── Configuration ─────────────────────────────────────────────────────────────
GROUP = os.environ.get("GROUP", "core")
GROUP_NAME = os.environ.get("GROUP_NAME", GROUP)
DEVICE_TYPE = os.environ.get("DEVICE_TYPE", "core router")
DEVICE_VENDOR = os.environ.get("DEVICE_VENDOR", "Cisco")
DEVICE_COUNT = int(os.environ.get("DEVICE_COUNT", "3"))
INTERFACES_PER_DEVICE = int(os.environ.get("INTERFACES_PER_DEVICE", "4"))
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "site-core:4317")
SCRAPE_INTERVAL_S = float(os.environ.get("SCRAPE_INTERVAL_S", "30"))
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0.08"))
INTENSITY = int(os.environ.get("INTENSITY", "2"))
SITE = "dc-1"

# ── Device profiles ───────────────────────────────────────────────────────────
PROFILES = {
    "core": {
        "device_prefix": "core-rtr",
        "iface_prefixes": ["HundredGigE", "FortyGigE", "TenGigE", "GigabitEthernet"],
        "bandwidth_bps": 100_000_000_000,
        "has_latency": False,
        "has_fw": False,
        "has_lb": False,
    },
    "edge": {
        "device_prefix": "edge-rtr",
        "iface_prefixes": ["GigabitEthernet", "Serial", "TenGigE", "Tunnel"],
        "bandwidth_bps": 10_000_000_000,
        "has_latency": True,
        "has_fw": False,
        "has_lb": False,
    },
    "fw": {
        "device_prefix": "fw",
        "iface_prefixes": ["ethernet", "ae", "loopback", "management"],
        "bandwidth_bps": 25_000_000_000,
        "has_latency": False,
        "has_fw": True,
        "has_lb": False,
    },
    "lb": {
        "device_prefix": "lb",
        "iface_prefixes": ["mgmt", "internal", "external", "ha"],
        "bandwidth_bps": 40_000_000_000,
        "has_latency": False,
        "has_fw": False,
        "has_lb": True,
    },
}

# ── Log message templates (Cisco/Juniper style) ───────────────────────────────
def _iface_name():
    prefixes = ["GigabitEthernet", "TenGigE", "HundredGigE", "Serial", "Tunnel", "ae", "eth"]
    return f"{random.choice(prefixes)}{random.randint(0, 3)}/{random.randint(0, 47)}"


def _ip():
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def _bgp_asn():
    return random.choice([65001, 65002, 65100, 65200, 64512, 7018, 3356])


LOG_TEMPLATES = [
    # (severity_name, log_level, event_type, message_fn)
    ("INFO",     logging.INFO,    "link_up",
     lambda: f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {_iface_name()}, changed state to up"),
    ("WARNING",  logging.WARNING, "link_down",
     lambda: f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {_iface_name()}, changed state to down"),
    ("INFO",     logging.INFO,    "acl_deny",
     lambda: (f"%SEC-6-IPACCESSLOGP: list ACL-DENY-INBOUND denied "
              f"{random.choice(['tcp','udp','icmp'])} {_ip()}({random.randint(1024,65535)}) -> "
              f"{_ip()}({random.choice([22,23,80,443,3389,8080])}), {random.randint(1,50)} packets")),
    ("INFO",     logging.INFO,    "bgp_up",
     lambda: f"%BGP-5-ADJCHANGE: neighbor {_ip()} vpn vrf default Up"),
    ("WARNING",  logging.WARNING, "bgp_down",
     lambda: f"%BGP-5-ADJCHANGE: neighbor {_ip()} vpn vrf default Down"),
    ("INFO",     logging.INFO,    "config_change",
     lambda: f"%SYS-5-CONFIG_I: Configured from console by admin on vty0 ({_ip()})"),
    ("WARNING",  logging.WARNING, "crc_error",
     lambda: f"%LINK-4-ERROR: Interface {_iface_name()}, CRC error count {random.randint(10,500)} in last 60 seconds"),
    ("WARNING",  logging.WARNING, "high_cpu",
     lambda: f"%CPU-4-HIGH: CPU utilization {random.randint(80,99)}% for the last {random.choice([5,10,30])} seconds"),
    ("ERROR",    logging.ERROR,   "link_down_error",
     lambda: f"%LINK-3-UPDOWN: Interface {_iface_name()}, changed state to down"),
    ("ERROR",    logging.ERROR,   "bgp_notification",
     lambda: f"%BGP-3-NOTIFICATION: received from neighbor {_ip()} 6/2 (peer in wrong AS) {random.randint(1,4)} bytes"),
    ("CRITICAL", logging.CRITICAL, "fan_failed",
     lambda: f"%ENVMON-3-FAN_FAILED: Fan {random.randint(1,4)} failed in module {random.randint(1,4)}"),
    ("INFO",     logging.INFO,    "ospf_up",
     lambda: f"%OSPF-5-ADJCHG: Process 1, Nbr {_ip()} on {_iface_name()} from LOADING to FULL, Loading Done"),
    ("WARNING",  logging.WARNING, "ospf_down",
     lambda: f"%OSPF-4-ADJCHG: Process 1, Nbr {_ip()} on {_iface_name()} from FULL to DOWN, Dead timer expired"),
]

SEVERITY_WEIGHTS = [50, 20, 15, 10, 8, 10, 12, 10, 8, 5, 2, 8, 8]

# ── Change operation types (for traces) ──────────────────────────────────────
OPERATION_TYPES = ["config_change", "route_update"]
CHANGE_DETAILS = {
    "config_change": [
        "vlan-provision", "acl-update", "interface-config", "qos-policy", "static-route",
    ],
    "route_update": [
        "bgp-policy", "ospf-area-add", "ip-prefix-add", "route-redistribution",
    ],
}


# ── Metric helpers ────────────────────────────────────────────────────────────

def _dim_safe(s: str) -> str:
    """Sanitise a string for use as a DT line-protocol dimension value."""
    return s.lower().replace(" ", "-").replace("/", "-").replace(",", "").replace("_", "-")


# ─────────────────────────────────────────────────────────────────────────────
# Per-device simulator class
# ─────────────────────────────────────────────────────────────────────────────

class DeviceSimulator:
    """Holds OTel log + trace providers for one simulated network device."""

    def __init__(self, device_id: str, profile: dict):
        self.device_id = device_id
        self.profile = profile

        # Shared resource attributes — IDENTICAL across logs and traces
        resource = Resource.create({
            "service.namespace": "networking",
            "service.name": device_id,
            "host.name": device_id,
            "device.id": device_id,
            "device.type": DEVICE_TYPE,
            "device.vendor": DEVICE_VENDOR,
            "net.devgroup": GROUP,
            "net.site": SITE,
        })

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
        self._logger = logging.getLogger(f"device.{device_id}")
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

        # Keep names aligned with previous metrics where practical.
        self._m_cpu = self._meter.create_histogram("device.cpu.utilization")
        self._m_mem = self._meter.create_histogram("device.memory.utilization")
        self._c_uptime = self._meter.create_counter("device.uptime")
        self._m_rtt = self._meter.create_histogram("net.latency.rtt")
        self._m_packet_loss = self._meter.create_histogram("net.packet.loss")
        self._m_fw_sessions = self._meter.create_histogram("firewall.sessions.active")
        self._m_fw_throughput = self._meter.create_histogram("firewall.throughput")
        self._m_lb_connections = self._meter.create_histogram("lb.connections.active")
        self._m_lb_pool_up = self._meter.create_histogram("lb.pool.members.up")

        self._c_if_in_octets = self._meter.create_counter("net.interface.in.octets")
        self._c_if_out_octets = self._meter.create_counter("net.interface.out.octets")
        self._c_if_in_errors = self._meter.create_counter("net.interface.in.errors")
        self._c_if_out_discards = self._meter.create_counter("net.interface.out.discards")
        self._m_if_oper_status = self._meter.create_histogram("net.interface.oper_status")
        self._m_if_bw_util = self._meter.create_histogram("net.interface.bandwidth.utilization")

        # ── Interface state ───────────────────────────────────────────────
        iface_prefixes = profile["iface_prefixes"]
        self._interfaces: list[dict] = []
        for j in range(INTERFACES_PER_DEVICE):
            prefix = iface_prefixes[j % len(iface_prefixes)]
            self._interfaces.append({
                "name": f"{prefix}{j + 1}",
                "in_octets": random.randint(0, 10_000_000_000),
                "out_octets": random.randint(0, 10_000_000_000),
                "in_errors": 0,
                "out_discards": 0,
                "oper_status": 1,
                "offset": random.uniform(0, 2 * math.pi),
            })

        # ── Device state ──────────────────────────────────────────────────
        self._uptime_s = float(random.randint(86400, 86400 * 365))
        self._cpu_util = 25.0
        self._mem_util = 40.0
        self._rtt_ms = 4.0
        self._packet_loss_pct = 0.0
        self._fw_sessions = 80_000
        self._fw_throughput = 0.0
        self._lb_connections = 5000
        self._lb_pool_up = DEVICE_COUNT * 2
        self._iface_state: dict[str, dict] = {}
        self._cycle_offset = random.uniform(0, 2 * math.pi)

        # Delta accumulators for counter metrics (track increments per interval)
        self._prev_in_octets: dict[str, int] = {}
        self._prev_out_octets: dict[str, int] = {}
        self._prev_in_errors: dict[str, int] = {}
        self._prev_out_discards: dict[str, int] = {}
        self._prev_uptime_s: float = self._uptime_s

    # ── Simulation tick ───────────────────────────────────────────────────────

    def tick(self, t: float):
        """Update simulated values for the current time step t (monotonic seconds)."""
        phase = t / 300.0 + self._cycle_offset
        fault = random.random() < FAILURE_RATE
        bw_bps = self.profile["bandwidth_bps"]

        # CPU + memory: sinusoidal with noise; spike on fault
        self._cpu_util = max(0.0, min(100.0,
            25.0 + 15.0 * math.sin(phase) + random.gauss(0, 3.0)
            + (random.uniform(30, 55) if fault else 0.0)
        ))
        self._mem_util = max(0.0, min(100.0,
            40.0 + 10.0 * math.sin(phase * 0.7) + random.gauss(0, 2.0)
            + (random.uniform(15, 30) if fault else 0.0)
        ))
        self._uptime_s += SCRAPE_INTERVAL_S

        if self.profile["has_latency"]:
            self._rtt_ms = max(0.5,
                4.0 + 2.0 * math.sin(phase * 1.3) + random.gauss(0, 0.8)
                + (random.uniform(20, 80) if fault else 0.0)
            )
            self._packet_loss_pct = max(0.0,
                random.gauss(0, 0.1) + (random.uniform(1, 15) if fault else 0.0)
            )

        if self.profile["has_fw"]:
            self._fw_sessions = max(0, int(
                80_000 + 20_000 * math.sin(phase) + random.gauss(0, 2000)
                - (random.uniform(20000, 60000) if fault else 0)
            ))
            self._fw_throughput = max(0.0,
                bw_bps * 0.35 + bw_bps * 0.1 * math.sin(phase)
                + random.gauss(0, bw_bps * 0.01)
                - (bw_bps * 0.2 if fault else 0.0)
            )

        if self.profile["has_lb"]:
            self._lb_connections = max(0, int(
                5000 + 2000 * math.sin(phase) + random.gauss(0, 300)
                - (random.uniform(1000, 4000) if fault else 0)
            ))
            self._lb_pool_up = max(0,
                DEVICE_COUNT * 2 - (random.randint(1, 3) if fault else 0)
            )

        # Interface-level metrics
        new_iface_state: dict[str, dict] = {}
        for iface in self._interfaces:
            i_phase = phase + iface["offset"]
            oper_up = 1 if (not fault or random.random() > 0.3) else 0

            bw_fraction = max(0.0, min(1.0,
                0.3 + 0.2 * math.sin(i_phase) + random.gauss(0, 0.04)
                + (random.uniform(-0.2, 0.1) if fault else 0.0)
            ))
            bytes_in = bw_bps / 8 * SCRAPE_INTERVAL_S * bw_fraction
            bytes_out = bytes_in * random.uniform(0.4, 0.9)
            iface["in_octets"] += int(max(0, bytes_in + random.gauss(0, bytes_in * 0.05)))
            iface["out_octets"] += int(max(0, bytes_out + random.gauss(0, bytes_out * 0.05)))
            iface["in_errors"] += random.choices(
                [0, 0, 0, 1, random.randint(2, 10)], weights=[80, 8, 4, 5, 3])[0]
            iface["out_discards"] += random.choices(
                [0, 0, 1, random.randint(2, 5)], weights=[85, 8, 4, 3])[0]
            iface["oper_status"] = oper_up
            new_iface_state[iface["name"]] = {
                "in_octets": iface["in_octets"],
                "out_octets": iface["out_octets"],
                "in_errors": iface["in_errors"],
                "out_discards": iface["out_discards"],
                "oper_status": oper_up,
                "bw_util": round(min(100.0, bw_fraction * 100.0), 2),
            }
        self._iface_state = new_iface_state

        _root_log.info(
            "group=%s device=%s cpu=%.1f%% mem=%.1f%% fault=%s",
            GROUP, self.device_id, self._cpu_util, self._mem_util, fault,
        )

        # Emit a log record this tick (OTel logs SDK → edge collector)
        self._emit_log(fault)

        # Emit metrics this tick (OTel Metrics SDK → edge collector)
        self.emit_metrics()

    # ── Metric line-protocol builder ──────────────────────────────────────────

    def emit_metrics(self) -> None:
        """Record current device and interface metrics via OTel instruments."""
        dev_type_slug = _dim_safe(DEVICE_TYPE)
        dev_vendor_slug = _dim_safe(DEVICE_VENDOR)

        dev_attrs = {
            "device.id": self.device_id,
            "device.type": dev_type_slug,
            "device.vendor": dev_vendor_slug,
            "net.devgroup": GROUP,
            "net.site": SITE,
        }

        self._m_cpu.record(round(self._cpu_util, 2), dev_attrs)
        self._m_mem.record(round(self._mem_util, 2), dev_attrs)

        uptime_delta = max(0, int(self._uptime_s - self._prev_uptime_s))
        self._c_uptime.add(uptime_delta, dev_attrs)
        self._prev_uptime_s = self._uptime_s

        if self.profile["has_latency"]:
            self._m_rtt.record(round(self._rtt_ms, 3), dev_attrs)
            self._m_packet_loss.record(round(self._packet_loss_pct, 4), dev_attrs)

        if self.profile["has_fw"]:
            self._m_fw_sessions.record(self._fw_sessions, dev_attrs)
            self._m_fw_throughput.record(round(self._fw_throughput, 2), dev_attrs)

        if self.profile["has_lb"]:
            self._m_lb_connections.record(self._lb_connections, dev_attrs)
            self._m_lb_pool_up.record(self._lb_pool_up, dev_attrs)

        for iface_name, iv in self._iface_state.items():
            iface_slug = iface_name.replace("/", "-").replace(" ", "-")
            iface_attrs = {
                **dev_attrs,
                "interface": iface_slug,
            }

            prev_in = self._prev_in_octets.get(iface_name, iv["in_octets"])
            prev_out = self._prev_out_octets.get(iface_name, iv["out_octets"])
            prev_err = self._prev_in_errors.get(iface_name, iv["in_errors"])
            prev_disc = self._prev_out_discards.get(iface_name, iv["out_discards"])

            delta_in = max(0, iv["in_octets"] - prev_in)
            delta_out = max(0, iv["out_octets"] - prev_out)
            delta_err = max(0, iv["in_errors"] - prev_err)
            delta_disc = max(0, iv["out_discards"] - prev_disc)

            self._prev_in_octets[iface_name] = iv["in_octets"]
            self._prev_out_octets[iface_name] = iv["out_octets"]
            self._prev_in_errors[iface_name] = iv["in_errors"]
            self._prev_out_discards[iface_name] = iv["out_discards"]

            self._c_if_in_octets.add(delta_in, iface_attrs)
            self._c_if_out_octets.add(delta_out, iface_attrs)
            self._c_if_in_errors.add(delta_err, iface_attrs)
            self._c_if_out_discards.add(delta_disc, iface_attrs)
            self._m_if_oper_status.record(iv["oper_status"], iface_attrs)
            self._m_if_bw_util.record(iv["bw_util"], iface_attrs)

    # ── Log emission ──────────────────────────────────────────────────────────

    def _emit_log(self, fault: bool):
        """Emit one Cisco/Juniper-style structured log record via OTel Logs SDK."""
        weights = list(SEVERITY_WEIGHTS)
        if fault:
            weights[8] = 30  # link_down_error
            weights[10] = 10  # fan_failed
            weights[7] = 20   # high_cpu
        template = random.choices(LOG_TEMPLATES, weights=weights)[0]
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
            "log_emit device=%s event=%s severity=%s msg=%r",
            self.device_id, event_type, sev_name, message[:80],
        )

    # ── Trace emission ────────────────────────────────────────────────────────

    def emit_trace(self):
        """Emit one network_operation trace with child spans."""
        op_type = random.choice(OPERATION_TYPES)
        change_detail = random.choice(CHANGE_DETAILS[op_type])
        change_id = f"CHG-{uuid.uuid4().hex[:8].upper()}"
        vlan = random.choice(list(range(100, 200)) + list(range(300, 400)))
        prefix = (f"10.{random.randint(0,255)}.{random.randint(0,255)}.0/24")
        do_fail = random.random() < FAILURE_RATE

        with self._tracer.start_as_current_span("network_operation") as root:
            root.set_attribute("change.id", change_id)
            root.set_attribute("operation.type", op_type)
            root.set_attribute("device.id", self.device_id)
            root.set_attribute("net.devgroup", GROUP)
            root.set_attribute("net.site", SITE)
            root.set_attribute("vlan", vlan)
            root.set_attribute("prefix", prefix)
            root.set_attribute("change.detail", change_detail)
            time.sleep(random.uniform(0.005, 0.02))

            with self._tracer.start_as_current_span("validate") as span:
                span.set_attribute("change.id", change_id)
                span.set_attribute("device.id", self.device_id)
                span.set_attribute("validation.checks", random.randint(3, 8))
                time.sleep(random.uniform(0.01, 0.05))

            with self._tracer.start_as_current_span("reserve") as span:
                span.set_attribute("change.id", change_id)
                span.set_attribute("device.id", self.device_id)
                span.set_attribute("resource.type", "vlan+ip")
                span.set_attribute("vlan", vlan)
                span.set_attribute("prefix", prefix)
                time.sleep(random.uniform(0.005, 0.03))

            with self._tracer.start_as_current_span("push") as span:
                span.set_attribute("change.id", change_id)
                span.set_attribute("device.id", self.device_id)
                span.set_attribute("change.detail", change_detail)
                if do_fail:
                    span.set_status(Status(StatusCode.ERROR, "config push rejected"))
                    span.record_exception(
                        RuntimeError(f"Device {self.device_id}: config conflict on {change_detail}")
                    )
                time.sleep(random.uniform(0.05, 0.15))

            if do_fail:
                with self._tracer.start_as_current_span("rollback") as span:
                    span.set_attribute("change.id", change_id)
                    span.set_attribute("device.id", self.device_id)
                    span.set_attribute("reason", "push-rejected")
                    span.set_status(Status(StatusCode.ERROR, "rollback after push failure"))
                    time.sleep(random.uniform(0.03, 0.10))
                root.set_status(Status(StatusCode.ERROR, "operation failed — rolled back"))
                root.set_attribute("outcome", "rollback")
            else:
                with self._tracer.start_as_current_span("verify") as span:
                    span.set_attribute("change.id", change_id)
                    span.set_attribute("device.id", self.device_id)
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
    """Background thread: emit traces for each device at ~SCRAPE_INTERVAL_S cadence."""
    time.sleep(random.uniform(5, 15))
    while True:
        for sim in simulators:
            try:
                sim.emit_trace()
            except Exception as exc:
                _root_log.error("trace error device=%s: %s", sim.device_id, exc)
            time.sleep(random.uniform(0.5, 2.0))
        time.sleep(max(1.0, SCRAPE_INTERVAL_S - len(simulators) * 1.5))


def main():
    profile = PROFILES.get(GROUP)
    if profile is None:
        _root_log.error("Unknown GROUP=%s; valid: %s", GROUP, list(PROFILES.keys()))
        raise SystemExit(1)

    _root_log.info(
        "device-sim starting: group=%s type=%r vendor=%r devices=%d ifaces/dev=%d "
        "endpoint=%s interval=%ss failure_rate=%.0f%% intensity=%d",
        GROUP, DEVICE_TYPE, DEVICE_VENDOR, DEVICE_COUNT, INTERFACES_PER_DEVICE,
        OTLP_ENDPOINT, SCRAPE_INTERVAL_S, FAILURE_RATE * 100, INTENSITY,
    )

    # Build one simulator per device
    simulators: list[DeviceSimulator] = []
    dev_prefix = profile["device_prefix"]
    for i in range(DEVICE_COUNT):
        device_id = f"{dev_prefix}-{i + 1:02d}"
        sim = DeviceSimulator(device_id=device_id, profile=profile)
        simulators.append(sim)
        _root_log.info("  device registered: %s  type=%s vendor=%s", device_id, DEVICE_TYPE, DEVICE_VENDOR)

    # Start trace background thread
    t = threading.Thread(target=trace_loop, args=(simulators,), daemon=True, name="trace-loop")
    t.start()

    # Main loop: tick each device each interval → updates state + emits logs + metrics.
    elapsed = 0.0
    while True:
        time.sleep(SCRAPE_INTERVAL_S)
        elapsed += SCRAPE_INTERVAL_S
        for sim in simulators:
            try:
                sim.tick(elapsed)
            except Exception as exc:
                _root_log.error("tick error device=%s: %s", sim.device_id, exc)


if __name__ == "__main__":
    main()
