# SenGestion - SaaS de gestion pour PME

Projet de fin de formation - Titre professionnel **Développeur Web et Web Mobile (DWWM)**.

## Architecture

Application **Flask (Python)** en architecture **MVC**, avec **deux bases de données** :

| Base | Rôle | Compétence DWWM |
|---|---|---|
| **MySQL** (relationnel) | Cœur métier : utilisateurs, clients, devis, factures… | CP5 + SQL de CP6 |
| **MongoDB** (NoSQL) | Journaux d'activité, données IA, historiques | NoSQL de CP6 |

## Stack technique

- **Back-end** : Python, Flask, SQLAlchemy (MySQL), PyMongo (MongoDB)
- **Front-end** : HTML5, CSS3, JavaScript, Jinja2 - responsive, accessible **RGAA**
- **Sécurité** : Flask-Login (auth), hachage des mots de passe scrypt (via werkzeug), protection **CSRF**, requêtes paramétrées
- **Migrations** : Flask-Migrate
- **Déploiement** : Docker + docker-compose (web, MySQL, MongoDB), reverse proxy Caddy (TLS/HSTS), variables d'environnement (`.env`)

## Charte graphique (conforme RGAA)

- Marine `#021A3D` · Or `#F2B10E` · Jaune pâle `#E8E7A2`
- Règle : l'or est une couleur de **fond** (texte marine dessus, 9.09:1). Pour du texte or sur clair → `#8F6600` (4.5:1).

## Installation

```bash
# 1. Environnement virtuel
python -m venv venv
source venv/bin/activate        # macOS/Linux

# 2. Dépendances
pip install -r requirements.txt

# 3. Configuration
cp .env.example .env            # puis renseigner les identifiants

# 4. Bases de données
#    - MySQL : créer la base `sengestion`
#    - MongoDB : lancer un serveur local (mongodb://localhost:27017)
python init_db.py               # crée les tables + un compte admin

# 5. Lancer
python run.py                   # http://localhost:5002
```

Compte de test : `admin@sengestion.sn` / `admin1234` (à changer).

## Structure

```
app/
├── run.py                 # point d'entrée
├── init_db.py             # création BDD + admin
├── config/settings.py     # config (MySQL + MongoDB)
├── app/
│   ├── __init__.py        # application factory
│   ├── extensions.py      # db, mongo, login, csrf
│   ├── models/            # entités MySQL (SQLAlchemy) - CP5
│   ├── routes/            # contrôleurs (blueprints) - CP7
│   ├── services/          # logique métier + accès NoSQL - CP6
│   ├── templates/         # vues Jinja2 - CP3
│   └── static/css/        # tokens.css (charte RGAA) + styles.css
└── requirements.txt
```
