"""device-sim — unified per-asset-group simulator emitting logs + traces + metrics via OTLP.

One image, six compose services (one per asset group: pos, payments, inventory, fridge,
network, digital). Each container simulates DEVICE_COUNT assets in its group. For EACH
asset, a dedicated OTel Resource + LoggerProvider + TracerProvider is built so logs and
traces carry IDENTICAL resource attributes, enabling Dynatrace to correlate them into a
single entity per asset.

Metrics are emitted with the OpenTelemetry Metrics SDK over OTLP gRPC to the same edge
collector endpoint used by logs and traces.

Env vars:
  GROUP                     asset group (pos|payments|inventory|fridge|network|digital)
  GROUP_NAME                human-readable group label
  ASSET_TYPE                asset type string (e.g. "pos terminal")
  ASSET_VENDOR              vendor string (e.g. "NCR")
  DEVICE_COUNT              number of assets to simulate per container (default 3)
  OTEL_EXPORTER_OTLP_ENDPOINT   gRPC endpoint for the edge collector (e.g. pos:4317)
  SCRAPE_INTERVAL_S         metric publish interval in seconds (default 30)
  FAILURE_RATE              fault injection rate 0.0-1.0 (default 0.08)
  INTENSITY                 1=minimal 2=standard 3=high (default 2)
Signal contract:
  METRICS  — OTel metrics over OTLP gRPC to edge collector.
  LOGS     — Retail-style structured log records via OTel Logs SDK → edge collector.
  TRACES   — "customer_transaction" spans (purchase | return | void | exchange)
             scan -> totalize -> tender -> authorize -> settle -> receipt
             (or decline -> notify_customer on fail) → edge collector.
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
GROUP = os.environ.get("GROUP", "pos")
GROUP_NAME = os.environ.get("GROUP_NAME", GROUP)
ASSET_TYPE = os.environ.get("ASSET_TYPE", "pos terminal")
ASSET_VENDOR = os.environ.get("ASSET_VENDOR", "NCR")
DEVICE_COUNT = int(os.environ.get("DEVICE_COUNT", "3"))
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "pos:4317")
SCRAPE_INTERVAL_S = float(os.environ.get("SCRAPE_INTERVAL_S", "30"))
FAILURE_RATE = float(os.environ.get("FAILURE_RATE", "0.08"))
INTENSITY = int(os.environ.get("INTENSITY", "2"))
REGION = "region-east-1"
BANNER = "Banner-A"

# ── Asset profiles ───────────────────────────────────────────────────────────
# Each profile selects which metric families to emit and which logs to weight up.
# Per-asset numerical baselines are randomized in DeviceSimulator.__init__.
PROFILES = {
    "pos": {
        "asset_prefix": "pos",
        "emit": {"pos": True},
        "log_set": "pos",
    },
    "payments": {
        "asset_prefix": "pay",
        "emit": {"payments": True},
        "log_set": "payments",
    },
    "inventory": {
        "asset_prefix": "scn",
        "emit": {"inventory": True},
        "log_set": "inventory",
    },
    "fridge": {
        "asset_prefix": "frz",
        "emit": {"fridge": True},
        "log_set": "fridge",
    },
    "network": {
        "asset_prefix": "ap",
        "emit": {"network": True},
        "log_set": "network",
    },
    "digital": {
        "asset_prefix": "ecom",
        "emit": {"digital": True},
        "log_set": "digital",
    },
}

# ── Fridge sub-types (rotated when GROUP=fridge) ─────────────────────────────
# Each represents a different cold-chain category with its own temperature setpoint.
FRIDGE_SUBTYPES = [
    ("walk_in_cooler", 2.0, 5.0),      # produce / dairy walk-in: 2-5C
    ("walk_in_freezer", -22.0, -18.0), # ice cream / frozen meat: -22 to -18C
    ("display_cooler", 1.0, 4.0),      # grab-and-go beverages: 1-4C
    ("hvac_zone", 21.0, 23.0),         # sales-floor HVAC: ~22C
]

# ── Digital channel rotation (when GROUP=digital) ────────────────────────────
DIGITAL_CHANNELS = ["web_checkout", "mobile_app", "kiosk", "buy_online_pickup_store"]


# ── Helpers for log message content ─────────────────────────────────────────
def _ip():
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def _store_id():
    return f"store-{random.randint(1, 250):03d}"


def _ssid():
    return random.choice(["BannerA-Guest", "BannerA-Corp", "BannerA-POS", "BannerA-Backroom"])


def _tx_id():
    return f"TX-{uuid.uuid4().hex[:10].upper()}"


def _sku():
    return f"SKU-{random.randint(100000, 999999)}"


def _card_bin():
    return random.choice(["411111", "542012", "601100", "378282"])


def _decline_reason():
    return random.choice(["insufficient_funds", "do_not_honor", "expired_card",
                          "fraud_suspected", "card_velocity_exceeded", "issuer_unavailable"])


# Each tuple: (severity_name, log_level, event_type, message_fn).

_POS_LOGS = [
    ("INFO",     logging.INFO,    "checkout_complete",
     lambda: f"CHECKOUT-COMPLETE: tx={_tx_id()} basket_size={random.randint(1,40)} total_usd={random.uniform(3.50,420.00):.2f}"),
    ("INFO",     logging.INFO,    "lane_open",
     lambda: f"LANE-OPEN: lane={random.randint(1,24)} cashier=emp-{random.randint(1000,9999)}"),
    ("INFO",     logging.INFO,    "loyalty_lookup",
     lambda: f"LOYALTY-LOOKUP: tx={_tx_id()} member_id={random.randint(10000000,99999999)} tier={random.choice(['silver','gold','platinum'])}"),
    ("WARNING",  logging.WARNING, "scan_retry",
     lambda: f"SCAN-RETRY: tx={_tx_id()} sku={_sku()} attempt={random.randint(2,5)}"),
    ("WARNING",  logging.WARNING, "printer_low_paper",
     lambda: f"PRINTER-LOW-PAPER: lane={random.randint(1,24)} pct_remaining={random.randint(5,18)}%"),
    ("WARNING",  logging.WARNING, "drawer_open_alarm",
     lambda: f"DRAWER-OPEN-ALARM: lane={random.randint(1,24)} duration_s={random.randint(45,180)}"),
    ("ERROR",    logging.ERROR,   "printer_offline",
     lambda: f"PRINTER-OFFLINE: lane={random.randint(1,24)} model=EpsonTM-T88 status=disconnected"),
    ("ERROR",    logging.ERROR,   "emv_timeout",
     lambda: f"EMV-TIMEOUT: tx={_tx_id()} terminal=EMV-{random.randint(1,24)} elapsed_ms={random.randint(15000,32000)}"),
    ("ERROR",    logging.ERROR,   "drawer_jam",
     lambda: f"DRAWER-JAM: lane={random.randint(1,24)} cashier=emp-{random.randint(1000,9999)} requires_service=true"),
]

_PAYMENT_LOGS = [
    ("INFO",     logging.INFO,    "auth_approved",
     lambda: f"AUTH-APPROVED: tx={_tx_id()} bin={_card_bin()} amount_usd={random.uniform(3.50,420.00):.2f} latency_ms={random.randint(180,650)}"),
    ("INFO",     logging.INFO,    "settle_batch",
     lambda: f"SETTLE-BATCH: batch_id=B{random.randint(100000,999999)} count={random.randint(180,1200)} total_usd={random.uniform(8000,52000):.2f}"),
    ("INFO",     logging.INFO,    "refund_processed",
     lambda: f"REFUND-PROCESSED: tx={_tx_id()} bin={_card_bin()} amount_usd={random.uniform(3.50,180.00):.2f}"),
    ("WARNING",  logging.WARNING, "auth_slow",
     lambda: f"AUTH-SLOW: tx={_tx_id()} bin={_card_bin()} latency_ms={random.randint(1500,3500)} threshold_ms=1000"),
    ("WARNING",  logging.WARNING, "chargeback_received",
     lambda: f"CHARGEBACK-RECEIVED: tx={_tx_id()} bin={_card_bin()} reason_code={random.choice(['4855','4863','13.1','10.4'])}"),
    ("WARNING",  logging.WARNING, "terminal_offline_fallback",
     lambda: f"TERMINAL-OFFLINE-FALLBACK: terminal=EMV-{random.randint(1,24)} mode=store_and_forward"),
    ("ERROR",    logging.ERROR,   "payment_decline",
     lambda: f"PAYMENT-DECLINE: tx={_tx_id()} bin={_card_bin()} reason={_decline_reason()} issuer_code={random.randint(500,999)}"),
    ("ERROR",    logging.ERROR,   "fraud_block",
     lambda: f"FRAUD-BLOCK: tx={_tx_id()} bin={_card_bin()} risk_score={random.uniform(85,99):.1f} rule=velocity"),
    ("CRITICAL", logging.CRITICAL, "gateway_unreachable",
     lambda: f"GATEWAY-UNREACHABLE: provider={random.choice(['adyen','stripe','firstdata','worldpay'])} retries=5 last_seen_s={random.randint(60,600)}"),
]

_INVENTORY_LOGS = [
    ("INFO",     logging.INFO,    "rfid_inventory_scan",
     lambda: f"RFID-SCAN: scanner=scn-{random.randint(1,24):02d} tags_read={random.randint(120,1800)} duration_s={random.uniform(8.0,45.0):.1f}"),
    ("INFO",     logging.INFO,    "restock_complete",
     lambda: f"RESTOCK-COMPLETE: sku={_sku()} qty={random.randint(6,72)} aisle={random.randint(1,24)}"),
    ("INFO",     logging.INFO,    "audit_match",
     lambda: f"AUDIT-MATCH: sku={_sku()} system_qty={random.randint(0,80)} counted_qty={random.randint(0,80)} delta=0"),
    ("WARNING",  logging.WARNING, "low_stock",
     lambda: f"LOW-STOCK: sku={_sku()} on_hand={random.randint(0,6)} reorder_point={random.randint(8,24)}"),
    ("WARNING",  logging.WARNING, "scan_failure",
     lambda: f"SCAN-FAILURE: scanner=scn-{random.randint(1,24):02d} sku={_sku()} reason={random.choice(['barcode_damaged','rfid_collision','tag_not_in_db'])}"),
    ("WARNING",  logging.WARNING, "audit_discrepancy",
     lambda: f"AUDIT-DISCREPANCY: sku={_sku()} system_qty={random.randint(10,80)} counted_qty={random.randint(0,40)} delta={random.randint(-30,-1)}"),
    ("ERROR",    logging.ERROR,   "stockout",
     lambda: f"STOCKOUT: sku={_sku()} aisle={random.randint(1,24)} last_sold_min_ago={random.randint(2,180)}"),
    ("ERROR",    logging.ERROR,   "shrinkage_event",
     lambda: f"SHRINKAGE-EVENT: sku={_sku()} expected_qty={random.randint(20,90)} actual_qty={random.randint(0,15)} loss_usd={random.uniform(50,950):.2f}"),
]

# Fridge: temp excursions are the SIGNATURE event of this demo (food safety)
_FRIDGE_LOGS = [
    ("INFO",     logging.INFO,    "temp_normal",
     lambda: f"TEMP-NORMAL: unit=frz-{random.randint(1,12):02d} temp_c={random.uniform(-21.0,4.0):.2f} setpoint_c={random.choice([-20.0,2.5,21.0])}"),
    ("INFO",     logging.INFO,    "defrost_cycle_start",
     lambda: f"DEFROST-CYCLE-START: unit=frz-{random.randint(1,12):02d} duration_planned_min={random.randint(15,45)}"),
    ("INFO",     logging.INFO,    "compressor_cycle",
     lambda: f"COMPRESSOR-CYCLE: unit=frz-{random.randint(1,12):02d} state={random.choice(['on','off'])} amps={random.uniform(8.0,18.0):.2f}"),
    ("WARNING",  logging.WARNING, "door_left_open",
     lambda: f"DOOR-LEFT-OPEN: unit=frz-{random.randint(1,12):02d} duration_s={random.randint(60,600)} threshold_s=45"),
    ("WARNING",  logging.WARNING, "hvac_setpoint_drift",
     lambda: f"HVAC-SETPOINT-DRIFT: zone={random.randint(1,6)} actual_c={random.uniform(24.5,27.0):.2f} setpoint_c=22.0"),
    ("WARNING",  logging.WARNING, "compressor_high_amps",
     lambda: f"COMPRESSOR-HIGH-AMPS: unit=frz-{random.randint(1,12):02d} amps={random.uniform(20.0,26.0):.2f} threshold_amps=19.0"),
    ("ERROR",    logging.ERROR,   "temp_excursion",
     lambda: f"TEMP-EXCURSION: unit=frz-{random.randint(1,12):02d} temp_c={random.uniform(8.0,15.0):.2f} setpoint_c=2.5 duration_min={random.randint(15,90)} food_safety_alert=true"),
    ("ERROR",    logging.ERROR,   "compressor_fail",
     lambda: f"COMPRESSOR-FAIL: unit=frz-{random.randint(1,12):02d} model=Copeland-ZB66 last_amps={random.uniform(0,3):.2f} hours_run={random.randint(28000,52000)}"),
    ("CRITICAL", logging.CRITICAL, "spoilage_risk",
     lambda: f"SPOILAGE-RISK: unit=frz-{random.randint(1,12):02d} temp_c={random.uniform(12.0,20.0):.2f} duration_min={random.randint(120,360)} est_loss_usd={random.uniform(800,8500):.2f} hold_inventory=true"),
]

_NETWORK_LOGS = [
    ("INFO",     logging.INFO,    "client_associate",
     lambda: f"CLIENT-ASSOCIATE: ap=ap-{random.randint(1,12):02d} ssid={_ssid()} mac={'-'.join(f'{random.randint(0,255):02x}' for _ in range(6))} rssi_dbm={random.randint(-72,-45)}"),
    ("INFO",     logging.INFO,    "dhcp_lease",
     lambda: f"DHCP-LEASE: ap=ap-{random.randint(1,12):02d} client_ip={_ip()} lease_s={random.choice([3600,7200,28800])}"),
    ("INFO",     logging.INFO,    "wan_link_up",
     lambda: f"WAN-LINK-UP: edge=wan-{random.randint(1,4)} carrier={random.choice(['ATT','Verizon','Spectrum','Comcast'])} uplink_mbps={random.choice([100,250,500,1000])}"),
    ("WARNING",  logging.WARNING, "wifi_auth_fail",
     lambda: f"WIFI-AUTH-FAIL: ap=ap-{random.randint(1,12):02d} ssid={_ssid()} mac={'-'.join(f'{random.randint(0,255):02x}' for _ in range(6))} reason=eap-timeout"),
    ("WARNING",  logging.WARNING, "wan_link_flap",
     lambda: f"WAN-LINK-FLAP: edge=wan-{random.randint(1,4)} flaps_15m={random.randint(2,8)} carrier={random.choice(['ATT','Verizon','Spectrum'])}"),
    ("WARNING",  logging.WARNING, "captive_portal_error",
     lambda: f"CAPTIVE-PORTAL-ERROR: ap=ap-{random.randint(1,12):02d} ssid=BannerA-Guest http_code={random.choice([502,503,504])}"),
    ("ERROR",    logging.ERROR,   "dhcp_pool_exhausted",
     lambda: f"DHCP-POOL-EXHAUSTED: ap=ap-{random.randint(1,12):02d} ssid={_ssid()} pool_size={random.choice([254,510])} in_use={random.choice([254,510])}"),
    ("ERROR",    logging.ERROR,   "uplink_degraded",
     lambda: f"UPLINK-DEGRADED: edge=wan-{random.randint(1,4)} packet_loss_pct={random.uniform(3.0,9.0):.2f} jitter_ms={random.randint(40,180)}"),
    ("CRITICAL", logging.CRITICAL, "wan_link_down",
     lambda: f"WAN-LINK-DOWN: edge=wan-{random.randint(1,4)} carrier={random.choice(['ATT','Verizon','Spectrum'])} duration_s={random.randint(60,1800)} pos_offline_mode=true"),
]

_DIGITAL_LOGS = [
    ("INFO",     logging.INFO,    "add_to_cart",
     lambda: f"ADD-TO-CART: session={uuid.uuid4().hex[:12]} sku={_sku()} qty={random.randint(1,5)} channel={random.choice(DIGITAL_CHANNELS)}"),
    ("INFO",     logging.INFO,    "checkout_complete",
     lambda: f"ECOM-CHECKOUT-COMPLETE: order_id=ORD-{random.randint(10000000,99999999)} basket_size={random.randint(1,12)} total_usd={random.uniform(15.00,580.00):.2f}"),
    ("INFO",     logging.INFO,    "search_query",
     lambda: f"SEARCH-QUERY: session={uuid.uuid4().hex[:12]} q={random.choice(['blue jeans','running shoes','iphone case','organic milk','dog food'])!r} hits={random.randint(8,240)} latency_ms={random.randint(80,420)}"),
    ("WARNING",  logging.WARNING, "cart_abandon",
     lambda: f"CART-ABANDON: session={uuid.uuid4().hex[:12]} basket_size={random.randint(1,8)} basket_usd={random.uniform(20.00,320.00):.2f} stage={random.choice(['shipping','payment','review'])}"),
    ("WARNING",  logging.WARNING, "search_slow",
     lambda: f"SEARCH-SLOW: q={random.choice(['holiday lights','toys','laptop'])!r} latency_ms={random.randint(1500,4200)} threshold_ms=800"),
    ("WARNING",  logging.WARNING, "image_cdn_miss",
     lambda: f"CDN-MISS: asset=/img/product/{random.randint(1000,99999)}.jpg origin=us-east-1 origin_latency_ms={random.randint(400,1800)}"),
    ("ERROR",    logging.ERROR,   "ecom_5xx",
     lambda: f"ECOM-5XX: route=/checkout/{random.choice(['shipping','review','confirm'])} status={random.choice([500,502,503,504])} request_id=req-{uuid.uuid4().hex[:10]}"),
    ("ERROR",    logging.ERROR,   "payment_redirect_error",
     lambda: f"PAYMENT-REDIRECT-ERROR: session={uuid.uuid4().hex[:12]} provider={random.choice(['paypal','applepay','klarna','affirm'])} code={random.choice(['REDIRECT_FAILED','INVALID_TOKEN'])}"),
    ("CRITICAL", logging.CRITICAL, "checkout_outage",
     lambda: f"CHECKOUT-OUTAGE: region=us-east-1 affected_sessions={random.randint(800,15000)} duration_s={random.randint(60,1200)} status=investigating"),
]

LOG_SETS = {
    "pos": _POS_LOGS,
    "payments": _PAYMENT_LOGS,
    "inventory": _INVENTORY_LOGS,
    "fridge": _FRIDGE_LOGS,
    "network": _NETWORK_LOGS,
    "digital": _DIGITAL_LOGS,
}

# Default severity weights match the order of templates in each set above.
LOG_WEIGHTS = {
    "pos":       [60, 25, 30, 18, 15, 10, 5, 4, 2],
    "payments":  [70, 20, 30, 18, 12, 10, 8, 4, 1],
    "inventory": [50, 30, 35, 25, 18, 12, 6, 3],
    "fridge":    [55, 25, 35, 20, 18, 12, 6, 3, 1],
    "network":   [60, 50, 25, 18, 14, 12, 4, 3, 1],
    "digital":   [80, 40, 70, 18, 14, 10, 6, 4, 1],
}

# ── Trace operation types ────────────────────────────────────────────────────
OPERATION_TYPES = ["purchase", "return", "void", "exchange"]
TENDER_TYPES = ["credit", "debit", "gift_card", "ebt", "cash", "mobile_wallet"]


def _dim_safe(s: str) -> str:
    """Sanitise a string for use as a metric attribute value."""
    return s.lower().replace(" ", "-").replace("/", "-").replace(",", "").replace("_", "-").replace("&", "and")


# ─────────────────────────────────────────────────────────────────────────────
# Per-asset simulator class
# ─────────────────────────────────────────────────────────────────────────────

class DeviceSimulator:
    """Holds OTel log + trace + meter providers for one simulated retail asset."""

    def __init__(self, device_id: str, profile: dict, fridge_subtype: tuple | None = None,
                 digital_channel: str | None = None):
        self.device_id = device_id
        self.profile = profile
        self.fridge_subtype = fridge_subtype  # (name, min_c, max_c)
        self.digital_channel = digital_channel
        # Every asset belongs to a (simulated) store + banner — gives Dynatrace a store dim.
        self.store_id = f"store-{random.randint(1, 250):03d}"

        # Shared resource attributes — IDENTICAL across logs, traces, and metrics.
        attrs = {
            "service.namespace": "retail",
            "service.name": device_id,
            "host.name": device_id,
            "asset.id": device_id,
            "asset.type": ASSET_TYPE,
            "asset.vendor": ASSET_VENDOR,
            "retail.assetgroup": GROUP,
            "retail.region": REGION,
            "retail.store_id": self.store_id,
            "retail.banner": BANNER,
        }
        if fridge_subtype is not None:
            attrs["retail.fridge.subtype"] = fridge_subtype[0]
            attrs["retail.fridge.setpoint_c"] = (fridge_subtype[1] + fridge_subtype[2]) / 2.0
        if digital_channel is not None:
            attrs["retail.digital.channel"] = digital_channel
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

        # ── POS instruments ───────────────────────────────────────────────
        self._m_pos_txn_rate     = self._meter.create_histogram("retail.pos.txn_per_min")
        self._m_pos_basket_usd   = self._meter.create_histogram("retail.pos.basket_usd")
        self._m_pos_avg_scan_ms  = self._meter.create_histogram("retail.pos.avg_scan_ms")
        self._m_pos_printer_ok   = self._meter.create_histogram("retail.pos.printer_online")
        self._c_pos_voids        = self._meter.create_counter("retail.pos.void_count")

        # ── Payments instruments ──────────────────────────────────────────
        self._m_pay_auth_lat     = self._meter.create_histogram("retail.payment.auth_latency_ms")
        self._m_pay_approval     = self._meter.create_histogram("retail.payment.approval_rate")
        self._m_pay_decline      = self._meter.create_histogram("retail.payment.decline_rate")
        self._c_pay_chargebacks  = self._meter.create_counter("retail.payment.chargeback_count")
        self._m_pay_amount_usd   = self._meter.create_histogram("retail.payment.amount_usd")

        # ── Inventory instruments ─────────────────────────────────────────
        self._m_inv_scan_rate    = self._meter.create_histogram("retail.inventory.scan_per_min")
        self._m_inv_scan_fail    = self._meter.create_histogram("retail.inventory.scan_failure_rate")
        self._m_inv_rfid_reads   = self._meter.create_histogram("retail.inventory.rfid_tag_reads")
        self._c_inv_stockouts    = self._meter.create_counter("retail.inventory.stockout_count")
        self._m_inv_on_hand_pct  = self._meter.create_histogram("retail.inventory.on_hand_pct")

        # ── Fridge / HVAC instruments ─────────────────────────────────────
        self._m_fridge_temp      = self._meter.create_histogram("retail.fridge.temp_c")
        self._m_fridge_amps      = self._meter.create_histogram("retail.fridge.compressor_amps")
        self._c_fridge_door_open = self._meter.create_counter("retail.fridge.door_open_count")
        self._m_hvac_zone_c      = self._meter.create_histogram("retail.hvac.zone_temp_c")
        self._m_fridge_energy    = self._meter.create_histogram("retail.fridge.energy_kwh")

        # ── Network instruments ───────────────────────────────────────────
        self._m_net_clients      = self._meter.create_histogram("retail.network.client_count")
        self._m_net_uplink_mbps  = self._meter.create_histogram("retail.network.uplink_mbps")
        self._m_net_wan_lat      = self._meter.create_histogram("retail.network.wan_latency_ms")
        self._m_net_auth_fails   = self._meter.create_histogram("retail.network.wifi_auth_failures")
        self._m_net_rssi         = self._meter.create_histogram("retail.network.client_rssi_dbm")

        # ── Digital instruments ───────────────────────────────────────────
        self._m_dig_checkout_lat = self._meter.create_histogram("retail.ecom.checkout_latency_ms")
        self._m_dig_cart_usd     = self._meter.create_histogram("retail.ecom.cart_value_usd")
        self._m_dig_checkout_ok  = self._meter.create_histogram("retail.ecom.checkout_success_rate")
        self._m_dig_search_lat   = self._meter.create_histogram("retail.ecom.search_latency_ms")
        self._c_dig_cart_adds    = self._meter.create_counter("retail.ecom.add_to_cart_count")

        # ── Per-asset simulated state baselines ───────────────────────────
        self._cycle_offset = random.uniform(0, 2 * math.pi)
        # pos
        self._pos_txn_per_min = random.uniform(2.5, 12.0)
        self._pos_basket_usd = random.uniform(18.0, 95.0)
        self._pos_avg_scan_ms = random.uniform(280.0, 540.0)
        self._pos_printer_online = 1
        self._pos_void_total = 0
        # payments
        self._pay_auth_latency = random.uniform(250.0, 520.0)
        self._pay_approval_rate = random.uniform(0.965, 0.988)
        self._pay_decline_rate = 1.0 - self._pay_approval_rate
        self._pay_chargebacks_total = 0
        self._pay_amount_usd = self._pos_basket_usd
        # inventory
        self._inv_scan_per_min = random.uniform(45.0, 140.0)
        self._inv_scan_fail_rate = random.uniform(0.005, 0.025)
        self._inv_rfid_reads = random.uniform(120.0, 1400.0)
        self._inv_stockout_total = 0
        self._inv_on_hand_pct = random.uniform(72.0, 96.0)
        # fridge / hvac
        if fridge_subtype is not None:
            sub_name, sub_min, sub_max = fridge_subtype
            self._fridge_setpoint = (sub_min + sub_max) / 2.0
        else:
            self._fridge_setpoint = 2.5
        self._fridge_temp_c = self._fridge_setpoint + random.uniform(-0.5, 0.5)
        self._fridge_amps = random.uniform(9.0, 16.0)
        self._fridge_door_open_total = 0
        self._hvac_zone_c = random.uniform(21.5, 22.8)
        self._fridge_energy_kwh = random.uniform(8.0, 22.0)
        # network
        self._net_client_count = random.uniform(35.0, 180.0)
        self._net_uplink_mbps = random.choice([100.0, 250.0, 500.0, 1000.0])
        self._net_wan_latency_ms = random.uniform(12.0, 38.0)
        self._net_auth_fail_count = random.uniform(0.0, 4.0)
        self._net_rssi_dbm = random.uniform(-65.0, -48.0)
        # digital
        self._dig_checkout_latency = random.uniform(450.0, 1100.0)
        self._dig_cart_usd = random.uniform(35.0, 220.0)
        self._dig_checkout_success = random.uniform(0.92, 0.985)
        self._dig_search_latency = random.uniform(120.0, 380.0)
        self._dig_cart_adds_total = 0

        self._prev_pos_voids = 0
        self._prev_pay_chargebacks = 0
        self._prev_inv_stockouts = 0
        self._prev_fridge_door_open = 0
        self._prev_dig_cart_adds = 0

    # ── Simulation tick ───────────────────────────────────────────────────
    def tick(self, t: float):
        """Update simulated values for the current time step t (monotonic seconds)."""
        phase = t / 300.0 + self._cycle_offset
        fault = random.random() < FAILURE_RATE

        # ── POS ────────────────────────────────────────────────────────────
        # Diurnal traffic curve: lunch/evening rush amplifies basket counts.
        target_txn = 6.0 + 4.5 * max(0.0, math.sin(phase * 1.2))
        if fault and GROUP == "pos":
            target_txn *= random.uniform(0.4, 0.7)  # drawer jam / printer offline → lane slows
            if random.random() < 0.35:
                self._pos_printer_online = 0
                self._pos_void_total += random.randint(1, 3)
            else:
                self._pos_printer_online = 1
        else:
            self._pos_printer_online = 1
        self._pos_txn_per_min = max(0.0, 0.7 * self._pos_txn_per_min + 0.3 * target_txn
                                    + random.gauss(0, 0.4))
        self._pos_basket_usd = max(2.0, 45.0 + 18.0 * math.sin(phase * 0.7)
                                   + random.gauss(0, 8.0))
        self._pos_avg_scan_ms = max(150.0,
            380.0 + 60.0 * math.sin(phase * 1.8) + random.gauss(0, 25.0)
            + (random.uniform(300, 1200) if (fault and GROUP == "pos") else 0.0)
        )

        # ── Payments ──────────────────────────────────────────────────────
        baseline_lat = 380.0 + 80.0 * math.sin(phase * 0.9)
        self._pay_auth_latency = max(120.0,
            0.7 * self._pay_auth_latency + 0.3 * baseline_lat
            + random.gauss(0, 35.0)
            + (random.uniform(800, 2500) if (fault and GROUP == "payments") else 0.0)
        )
        baseline_approval = 0.976 + 0.003 * math.sin(phase * 0.4)
        self._pay_approval_rate = max(0.5, min(1.0,
            0.85 * self._pay_approval_rate + 0.15 * baseline_approval
            - (random.uniform(0.05, 0.15) if (fault and GROUP == "payments") else 0.0)
        ))
        self._pay_decline_rate = 1.0 - self._pay_approval_rate
        if fault and GROUP == "payments" and random.random() < 0.4:
            self._pay_chargebacks_total += random.randint(1, 3)
        self._pay_amount_usd = self._pos_basket_usd  # mirror POS basket

        # ── Inventory ──────────────────────────────────────────────────────
        self._inv_scan_per_min = max(0.0,
            85.0 + 35.0 * math.sin(phase * 1.1) + random.gauss(0, 12.0)
        )
        self._inv_scan_fail_rate = max(0.0, min(1.0,
            0.015 + random.gauss(0, 0.003)
            + (random.uniform(0.02, 0.08) if (fault and GROUP == "inventory") else 0.0)
        ))
        self._inv_rfid_reads = max(0.0,
            720.0 + 280.0 * math.sin(phase * 0.6) + random.gauss(0, 60.0)
        )
        if fault and GROUP == "inventory" and random.random() < 0.5:
            self._inv_stockout_total += random.randint(1, 4)
        self._inv_on_hand_pct = max(0.0, min(100.0,
            self._inv_on_hand_pct - (random.uniform(0.5, 2.5) if fault else random.uniform(0.0, 0.4))
            + random.uniform(0.0, 0.6)  # restocks keep it from monotonically falling
        ))

        # ── Fridge / HVAC (cold chain) ─────────────────────────────────────
        # Slow thermal drift toward setpoint + occasional excursions on fault.
        amb_drift = (self._hvac_zone_c - self._fridge_setpoint) * 0.005
        self._fridge_temp_c = (self._fridge_temp_c
                               + amb_drift
                               + random.gauss(0, 0.15)
                               - 0.02 * (self._fridge_temp_c - self._fridge_setpoint))
        if fault and GROUP == "fridge":
            # Temp excursion event — defining demo signal for cold chain.
            self._fridge_temp_c += random.uniform(2.0, 9.0)
        self._fridge_amps = max(0.0,
            12.0 + 2.5 * math.sin(phase * 1.4) + random.gauss(0, 0.6)
            + (random.uniform(4.0, 9.0) if (fault and GROUP == "fridge" and random.random() < 0.4) else 0.0)
        )
        if random.random() < 0.15 + (0.45 if (fault and GROUP == "fridge") else 0.0):
            self._fridge_door_open_total += 1
        self._hvac_zone_c = max(15.0, min(28.0,
            22.0 + 0.6 * math.sin(phase * 0.3) + random.gauss(0, 0.2)
            + (random.uniform(1.5, 4.0) if (fault and GROUP == "fridge") else 0.0)
        ))
        self._fridge_energy_kwh = max(0.0,
            14.0 + 4.0 * math.sin(phase * 0.5) + random.gauss(0, 0.8)
            + (random.uniform(2.0, 8.0) if (fault and GROUP == "fridge") else 0.0)
        )

        # ── Network ────────────────────────────────────────────────────────
        # Diurnal client population (more shoppers, more Wi-Fi clients).
        target_clients = 110.0 + 60.0 * max(0.0, math.sin(phase * 1.0))
        self._net_client_count = max(0.0,
            0.7 * self._net_client_count + 0.3 * target_clients + random.gauss(0, 6.0)
            - (random.uniform(30, 80) if (fault and GROUP == "network") else 0.0)
        )
        self._net_wan_latency_ms = max(2.0,
            22.0 + 8.0 * math.sin(phase * 0.8) + random.gauss(0, 3.0)
            + (random.uniform(80, 280) if (fault and GROUP == "network") else 0.0)
        )
        self._net_auth_fail_count = max(0.0,
            1.5 + random.gauss(0, 0.6)
            + (random.uniform(8, 35) if (fault and GROUP == "network") else 0.0)
        )
        self._net_rssi_dbm = max(-90.0, min(-30.0,
            -58.0 + 6.0 * math.sin(phase * 1.5) + random.gauss(0, 2.5)
            - (random.uniform(8, 20) if (fault and GROUP == "network") else 0.0)
        ))

        # ── Digital ────────────────────────────────────────────────────────
        baseline_co_lat = 720.0 + 220.0 * max(0.0, math.sin(phase * 0.9))
        self._dig_checkout_latency = max(150.0,
            0.7 * self._dig_checkout_latency + 0.3 * baseline_co_lat
            + random.gauss(0, 50.0)
            + (random.uniform(1200, 4000) if (fault and GROUP == "digital") else 0.0)
        )
        self._dig_cart_usd = max(5.0,
            120.0 + 50.0 * math.sin(phase * 0.7) + random.gauss(0, 20.0)
        )
        baseline_co_ok = 0.955 + 0.012 * math.sin(phase * 0.4)
        self._dig_checkout_success = max(0.4, min(1.0,
            0.85 * self._dig_checkout_success + 0.15 * baseline_co_ok
            - (random.uniform(0.05, 0.20) if (fault and GROUP == "digital") else 0.0)
        ))
        self._dig_search_latency = max(40.0,
            220.0 + 70.0 * math.sin(phase * 1.2) + random.gauss(0, 25.0)
            + (random.uniform(600, 2200) if (fault and GROUP == "digital") else 0.0)
        )
        # Cart-add counter ticks every cycle proportional to traffic.
        adds_this_cycle = max(0, int(self._net_client_count * random.uniform(0.05, 0.18)))
        self._dig_cart_adds_total += adds_this_cycle

        _root_log.info(
            "group=%s asset=%s store=%s txn/min=%.1f auth_ms=%.0f temp_c=%.2f clients=%.0f ecom_co_ms=%.0f fault=%s",
            GROUP, self.device_id, self.store_id, self._pos_txn_per_min,
            self._pay_auth_latency, self._fridge_temp_c, self._net_client_count,
            self._dig_checkout_latency, fault,
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
            "retail.assetgroup": GROUP,
            "retail.region": REGION,
            "retail.store_id": self.store_id,
            "retail.banner": BANNER,
        }
        if self.fridge_subtype is not None:
            asset_attrs["retail.fridge.subtype"] = self.fridge_subtype[0]
        if self.digital_channel is not None:
            asset_attrs["retail.digital.channel"] = self.digital_channel

        emit = self.profile["emit"]

        if emit.get("pos"):
            self._m_pos_txn_rate.record(round(self._pos_txn_per_min, 2), asset_attrs)
            self._m_pos_basket_usd.record(round(self._pos_basket_usd, 2), asset_attrs)
            self._m_pos_avg_scan_ms.record(round(self._pos_avg_scan_ms, 2), asset_attrs)
            self._m_pos_printer_ok.record(self._pos_printer_online, asset_attrs)
            delta = max(0, self._pos_void_total - self._prev_pos_voids)
            self._c_pos_voids.add(delta, asset_attrs)
            self._prev_pos_voids = self._pos_void_total

        if emit.get("payments"):
            self._m_pay_auth_lat.record(round(self._pay_auth_latency, 2), asset_attrs)
            self._m_pay_approval.record(round(self._pay_approval_rate, 5), asset_attrs)
            self._m_pay_decline.record(round(self._pay_decline_rate, 5), asset_attrs)
            delta = max(0, self._pay_chargebacks_total - self._prev_pay_chargebacks)
            self._c_pay_chargebacks.add(delta, asset_attrs)
            self._prev_pay_chargebacks = self._pay_chargebacks_total
            self._m_pay_amount_usd.record(round(self._pay_amount_usd, 2), asset_attrs)

        if emit.get("inventory"):
            self._m_inv_scan_rate.record(round(self._inv_scan_per_min, 2), asset_attrs)
            self._m_inv_scan_fail.record(round(self._inv_scan_fail_rate, 5), asset_attrs)
            self._m_inv_rfid_reads.record(round(self._inv_rfid_reads, 1), asset_attrs)
            delta = max(0, self._inv_stockout_total - self._prev_inv_stockouts)
            self._c_inv_stockouts.add(delta, asset_attrs)
            self._prev_inv_stockouts = self._inv_stockout_total
            self._m_inv_on_hand_pct.record(round(self._inv_on_hand_pct, 2), asset_attrs)

        if emit.get("fridge"):
            self._m_fridge_temp.record(round(self._fridge_temp_c, 2), asset_attrs)
            self._m_fridge_amps.record(round(self._fridge_amps, 2), asset_attrs)
            delta = max(0, self._fridge_door_open_total - self._prev_fridge_door_open)
            self._c_fridge_door_open.add(delta, asset_attrs)
            self._prev_fridge_door_open = self._fridge_door_open_total
            self._m_hvac_zone_c.record(round(self._hvac_zone_c, 2), asset_attrs)
            self._m_fridge_energy.record(round(self._fridge_energy_kwh, 2), asset_attrs)

        if emit.get("network"):
            self._m_net_clients.record(round(self._net_client_count, 1), asset_attrs)
            self._m_net_uplink_mbps.record(round(self._net_uplink_mbps, 1), asset_attrs)
            self._m_net_wan_lat.record(round(self._net_wan_latency_ms, 2), asset_attrs)
            self._m_net_auth_fails.record(round(self._net_auth_fail_count, 1), asset_attrs)
            self._m_net_rssi.record(round(self._net_rssi_dbm, 1), asset_attrs)

        if emit.get("digital"):
            self._m_dig_checkout_lat.record(round(self._dig_checkout_latency, 2), asset_attrs)
            self._m_dig_cart_usd.record(round(self._dig_cart_usd, 2), asset_attrs)
            self._m_dig_checkout_ok.record(round(self._dig_checkout_success, 5), asset_attrs)
            self._m_dig_search_lat.record(round(self._dig_search_latency, 2), asset_attrs)
            delta = max(0, self._dig_cart_adds_total - self._prev_dig_cart_adds)
            self._c_dig_cart_adds.add(delta, asset_attrs)
            self._prev_dig_cart_adds = self._dig_cart_adds_total

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
        """Emit one customer_transaction trace with the canonical retail span chain:
        scan -> totalize -> tender -> authorize -> (settle -> receipt) | (decline -> notify_customer)
        """
        op_type = random.choice(OPERATION_TYPES)
        tender = random.choice(TENDER_TYPES)
        tx_id = _tx_id()
        basket_size = random.randint(1, 28)
        basket_usd = round(random.uniform(3.50, 480.00), 2)
        # Higher decline odds for risky tenders + faults.
        decline_odds = FAILURE_RATE * (1.8 if tender in ("credit", "debit") else 1.0)
        do_fail = random.random() < decline_odds

        with self._tracer.start_as_current_span("customer_transaction") as root:
            root.set_attribute("transaction.id", tx_id)
            root.set_attribute("operation.type", op_type)
            root.set_attribute("asset.id", self.device_id)
            root.set_attribute("retail.assetgroup", GROUP)
            root.set_attribute("retail.region", REGION)
            root.set_attribute("retail.store_id", self.store_id)
            root.set_attribute("retail.banner", BANNER)
            root.set_attribute("basket.size", basket_size)
            root.set_attribute("basket.value_usd", basket_usd)
            root.set_attribute("tender.type", tender)
            time.sleep(random.uniform(0.005, 0.02))

            with self._tracer.start_as_current_span("scan") as span:
                span.set_attribute("transaction.id", tx_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("scan.items", basket_size)
                span.set_attribute("scan.method", random.choice(["barcode", "rfid", "self_scan"]))
                time.sleep(random.uniform(0.02, 0.08))

            with self._tracer.start_as_current_span("totalize") as span:
                span.set_attribute("transaction.id", tx_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("totalize.tax_usd", round(basket_usd * 0.07, 2))
                span.set_attribute("totalize.discount_usd", round(basket_usd * random.uniform(0.0, 0.18), 2))
                time.sleep(random.uniform(0.005, 0.02))

            with self._tracer.start_as_current_span("tender") as span:
                span.set_attribute("transaction.id", tx_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("tender.type", tender)
                time.sleep(random.uniform(0.01, 0.05))

            with self._tracer.start_as_current_span("authorize") as span:
                span.set_attribute("transaction.id", tx_id)
                span.set_attribute("asset.id", self.device_id)
                span.set_attribute("authorize.processor",
                                   random.choice(["adyen", "stripe", "firstdata", "worldpay", "fiserv"]))
                span.set_attribute("authorize.amount_usd", basket_usd)
                # Auth latency mirrors the metric on a per-trace basis.
                auth_ms = max(120, int(self._pay_auth_latency + random.gauss(0, 60)))
                span.set_attribute("authorize.latency_ms", auth_ms)
                if do_fail:
                    reason = _decline_reason()
                    span.set_attribute("authorize.decline_reason", reason)
                    span.set_status(Status(StatusCode.ERROR, f"declined: {reason}"))
                    span.record_exception(
                        RuntimeError(f"tx {tx_id}: {reason}")
                    )
                time.sleep(random.uniform(0.06, 0.2))

            if do_fail:
                with self._tracer.start_as_current_span("notify_customer") as span:
                    span.set_attribute("transaction.id", tx_id)
                    span.set_attribute("asset.id", self.device_id)
                    span.set_attribute("notify.channel", "terminal_display")
                    span.set_status(Status(StatusCode.ERROR, "transaction declined"))
                    time.sleep(random.uniform(0.02, 0.06))
                root.set_status(Status(StatusCode.ERROR, "transaction declined — customer notified"))
                root.set_attribute("outcome", "declined")
            else:
                with self._tracer.start_as_current_span("settle") as span:
                    span.set_attribute("transaction.id", tx_id)
                    span.set_attribute("asset.id", self.device_id)
                    span.set_attribute("settle.batch_window_min", 60)
                    time.sleep(random.uniform(0.02, 0.06))

                with self._tracer.start_as_current_span("receipt") as span:
                    span.set_attribute("transaction.id", tx_id)
                    span.set_attribute("asset.id", self.device_id)
                    span.set_attribute("receipt.format",
                                       random.choice(["printed", "email", "sms", "none"]))
                    time.sleep(random.uniform(0.01, 0.04))

                root.set_attribute("outcome", "approved")

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
        fridge_sub = None
        digital_ch = None
        if GROUP == "fridge":
            fridge_sub = FRIDGE_SUBTYPES[i % len(FRIDGE_SUBTYPES)]
        if GROUP == "digital":
            digital_ch = DIGITAL_CHANNELS[i % len(DIGITAL_CHANNELS)]
        sim = DeviceSimulator(device_id=device_id, profile=profile,
                              fridge_subtype=fridge_sub, digital_channel=digital_ch)
        simulators.append(sim)
        extra = ""
        if fridge_sub is not None:
            extra = f" subtype={fridge_sub[0]} setpoint_c={(fridge_sub[1]+fridge_sub[2])/2.0:.1f}"
        if digital_ch is not None:
            extra = f" channel={digital_ch}"
        _root_log.info(
            "  asset registered: %s  type=%s vendor=%s store=%s%s",
            device_id, ASSET_TYPE, ASSET_VENDOR, sim.store_id, extra,
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
