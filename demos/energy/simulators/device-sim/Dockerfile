FROM python:3.12-slim
LABEL org.opencontainers.image.description="Energy demo — unified per-asset-group simulator (metrics + logs + traces)"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sim.py .
CMD ["python", "sim.py"]
