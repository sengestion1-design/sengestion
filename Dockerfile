# SenGestion — image de l'application Flask
FROM python:3.12-slim

# ffmpeg : requis par Whisper (transcription vocale des devis/factures)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dépendances d'abord (cache Docker : ne se réinstalle que si requirements.txt change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code de l'application
COPY . .

# Dossier d'uploads (persisté via volume dans docker-compose)
RUN mkdir -p app/static/uploads/cartes app/static/uploads/recus app/static/uploads/settings

EXPOSE 5002

ENTRYPOINT ["./docker-entrypoint.sh"]
