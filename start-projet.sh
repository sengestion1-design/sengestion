#!/usr/bin/env bash
#
# start-projet.sh — Démarre tout l'environnement du projet final SenGestion
# --------------------------------------------------------------------------
# Lance dans l'ordre :
#   1. MongoDB   (logs IA / NoSQL — CP6)      -> port 27017
#   2. MySQL     (données métier — XAMPP)     -> port 3306   [demande sudo]
#   3. Serveur Flask (run.py)                 -> http://localhost:5002
#
# Usage :  ./start-projet.sh          (démarre tout)
#          ./start-projet.sh stop     (arrête tout)
#
set -u

# --- Chemins ---------------------------------------------------------------
APP_DIR="/Users/seck/Desktop/PROJET FINAL/app"
MONGO_BIN="$HOME/mongodb/bin/mongod"
MONGO_DATA="$HOME/mongodb-data/db"
MONGO_LOG="$HOME/mongodb-data/log/mongod.log"
MYSQL_CTL="/Applications/XAMPP/xamppfiles/bin/mysql.server"
FLASK_PORT=5002
MONGO_PORT=27017
MYSQL_PORT=3306

# --- Couleurs --------------------------------------------------------------
G="\033[0;32m"; Y="\033[0;33m"; R="\033[0;31m"; B="\033[0;34m"; N="\033[0m"
ok()   { echo -e "${G}✅ $1${N}"; }
info() { echo -e "${B}➜  $1${N}"; }
warn() { echo -e "${Y}⚠️  $1${N}"; }
err()  { echo -e "${R}❌ $1${N}"; }

port_open() { lsof -i ":$1" -sTCP:LISTEN -n -P >/dev/null 2>&1; }

# MySQL détecté par une VRAIE connexion (lsof n'est pas fiable pour le socket XAMPP)
MYSQL_CLIENT="/Applications/XAMPP/xamppfiles/bin/mysql"
mysql_up() { "$MYSQL_CLIENT" -u root -h 127.0.0.1 -e "SELECT 1;" >/dev/null 2>&1; }

# ==========================================================================
#  STOP
# ==========================================================================
if [ "${1:-}" = "stop" ]; then
  info "Arrêt de l'environnement SenGestion…"

  # Flask
  pkill -f "run.py" 2>/dev/null && ok "Serveur Flask arrêté." || warn "Flask n'était pas lancé."

  # MongoDB (arrêt propre)
  if port_open "$MONGO_PORT"; then
    "$MONGO_BIN" --dbpath "$MONGO_DATA" --shutdown 2>/dev/null \
      && ok "MongoDB arrêté." || warn "MongoDB : arrêt non confirmé."
  else
    warn "MongoDB n'était pas lancé."
  fi

  # MySQL (sudo)
  if port_open "$MYSQL_PORT"; then
    warn "Arrêt de MySQL (mot de passe requis) :"
    sudo "$MYSQL_CTL" stop && ok "MySQL arrêté."
  else
    warn "MySQL n'était pas lancé."
  fi

  echo ""
  ok "Environnement arrêté."
  exit 0
fi

# ==========================================================================
#  START
# ==========================================================================
echo -e "${B}╔══════════════════════════════════════════════╗${N}"
echo -e "${B}║   SenGestion — Démarrage de l'environnement   ║${N}"
echo -e "${B}╚══════════════════════════════════════════════╝${N}"
echo ""

# --- 1. MongoDB ------------------------------------------------------------
info "1/3 — MongoDB (logs IA / NoSQL)…"
if port_open "$MONGO_PORT"; then
  ok "MongoDB tourne déjà (port $MONGO_PORT)."
elif [ ! -x "$MONGO_BIN" ]; then
  err "mongod introuvable ($MONGO_BIN). Installation manquante."
  exit 1
else
  mkdir -p "$MONGO_DATA" "$(dirname "$MONGO_LOG")"
  "$MONGO_BIN" --dbpath "$MONGO_DATA" --logpath "$MONGO_LOG" \
               --port "$MONGO_PORT" --fork >/dev/null 2>&1
  # attendre l'ouverture du port (max ~5s)
  for i in {1..10}; do port_open "$MONGO_PORT" && break; sleep 0.5; done
  if port_open "$MONGO_PORT"; then ok "MongoDB démarré (port $MONGO_PORT)."
  else err "MongoDB n'a pas démarré — voir $MONGO_LOG"; exit 1; fi
fi

# --- 2. MySQL (XAMPP) ------------------------------------------------------
info "2/3 — MySQL (données métier — XAMPP)…"
if mysql_up; then
  ok "MySQL tourne déjà et répond (port $MYSQL_PORT)."
else
  warn "Démarrage de MySQL — mot de passe administrateur requis :"
  # Nettoie les .pid périmés (cause du 'A mysqld process already exists' après crash)
  sudo rm -f /Applications/XAMPP/xamppfiles/var/mysql/*.pid 2>/dev/null
  sudo "$MYSQL_CTL" start >/dev/null 2>&1
  # Attendre que MySQL réponde vraiment (max ~10s)
  for i in {1..20}; do mysql_up && break; sleep 0.5; done
  if mysql_up; then ok "MySQL démarré et opérationnel (port $MYSQL_PORT)."
  else err "MySQL ne répond pas. Ouvrez l'app XAMPP → Manage Servers → MySQL."; exit 1; fi
fi

# --- 3. Serveur Flask ------------------------------------------------------
info "3/3 — Serveur Flask…"
if port_open "$FLASK_PORT"; then
  warn "Un serveur tourne déjà sur le port $FLASK_PORT. Redémarrage…"
  pkill -f "run.py" 2>/dev/null
  sleep 1
fi

cd "$APP_DIR" || { err "Dossier introuvable : $APP_DIR"; exit 1; }
info "Lancement de run.py (Ctrl+C pour arrêter le serveur)…"
echo ""
ok "MongoDB + MySQL prêts. Ouvrez : http://localhost:$FLASK_PORT/"
echo ""
exec python3 run.py
