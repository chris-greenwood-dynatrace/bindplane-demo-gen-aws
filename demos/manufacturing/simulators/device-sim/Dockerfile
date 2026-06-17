FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy simulator
COPY sim.py .

# Default env (overridden by compose)
ENV LINE=A \
    LINE_NAME="Line A" \
    MACHINE_TYPE="CNC mill" \
    DEVICE_COUNT=3 \
    OTEL_EXPORTER_OTLP_ENDPOINT="localhost:4317" \
    SCRAPE_INTERVAL_S=30 \
    FAULT_RATE=0.08 \
    INTENSITY=2

CMD ["python", "-u", "sim.py"]
