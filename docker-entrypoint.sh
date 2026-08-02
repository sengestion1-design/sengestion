#!/bin/sh
# Point d'entrée du conteneur SenGestion :
# 1. attend que MySQL soit prêt, 2. crée les tables + admin, 3. lance gunicorn.
set -e

echo "Attente de MySQL (${DB_HOST}:${DB_PORT})..."
python - << 'PYEOF'
import os, time, pymysql
for i in range(60):
    try:
        pymysql.connect(
            host=os.getenv("DB_HOST", "db"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", "sengestion_flask_v2"),
        ).close()
        print("MySQL est prêt.")
        break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("MySQL injoignable après 120 s.")
PYEOF

echo "Initialisation de la base (tables + compte admin)..."
python init_db.py

echo "Démarrage de gunicorn..."
exec gunicorn --bind 0.0.0.0:5002 --workers 2 --timeout 120 "app:create_app('docker')"
