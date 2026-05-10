FROM python:3.11-slim

# Install ffmpeg (required for HLS video frame extraction)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements_cloud.txt .
RUN pip install --no-cache-dir -r requirements_cloud.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# gunicorn timeout = 9 min; set Cloud Run --timeout=540 to match
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "540", "--workers", "1", "cloud_run_main:app"]
