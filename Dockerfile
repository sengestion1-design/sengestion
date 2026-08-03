# SenGestion - Flask application image
FROM python:3.12-slim

# ffmpeg: required by Whisper (voice transcription of quotes/invoices)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (Docker cache: reinstalled only when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Upload folders (persisted through a docker-compose volume)
RUN mkdir -p app/static/uploads/cartes app/static/uploads/recus app/static/uploads/settings

EXPOSE 5002

ENTRYPOINT ["./docker-entrypoint.sh"]
