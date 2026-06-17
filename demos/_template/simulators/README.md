# Simulators

This directory contains the three simulator services required by the demo contract:
all three signals (metrics, logs, traces) must be present.

## What to put here

### 1. Metrics source (`metrics_sim.py` or similar)

A small Python script (stdlib + `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`)
that publishes metrics via OTLP to the gateway collector.

Key points:
- Export to `$OTLP_ENDPOINT` (defaults to `http://gateway:4318`)
- Use `$SCRAPE_INTERVAL_S` for the publish cadence
- Emit gauge or counter metrics representing the demo domain (e.g., sensor readings, device states)

### 2. Log generator (`log_gen.py` or similar)

A small Python script that writes structured JSON log lines to stdout (picked up by Docker
logging driver / filelog receiver) or sends OTLP logs directly.

Key points:
- Include `severity`, `body`, and resource attributes matching the demo domain
- Use `$LOG_INTERVAL_S` for the emit cadence
- Simulate domain events (faults, state changes, maintenance alerts)

### 3. Instrumented trace app (`trace_app.py` or similar)

A FastAPI or Flask application instrumented with the OpenTelemetry Python SDK. Emits traces
via OTLP HTTP to the gateway collector.

Key points:
- Export to `$OTLP_ENDPOINT`
- Set `service.name` to `$SERVICE_NAME`
- Simulate a realistic request flow (multi-span, parent/child) representing the demo domain
- Use `$FAULT_RATE` to inject errors (5xx responses) for demonstrating Dynatrace problem detection
- Expose a health endpoint at `/health` for liveness checks

## Dependencies

Add a `requirements.txt` here with the OTel packages:
```
opentelemetry-sdk>=1.25.0
opentelemetry-exporter-otlp-proto-http>=1.25.0
opentelemetry-instrumentation-fastapi>=0.46b0
fastapi>=0.111.0
uvicorn>=0.30.0
```
