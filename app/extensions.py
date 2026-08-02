"""Extensions partagées de l'application (initialisées dans la factory)."""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_mail import Mail
from pymongo import MongoClient

# --- MySQL / relationnel ---
db = SQLAlchemy()
migrate = Migrate()

# --- Auth & sécurité ---
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None
csrf = CSRFProtect()

# --- Email (Gmail SMTP) ---
mail = Mail()


class Mongo:
    """Petit wrapper autour de PyMongo, initialisé dans la factory.

    Usage : from app.extensions import mongo ; mongo.db.logs.insert_one(...)
    """

    client: MongoClient = None
    db = None

    def init_app(self, app):
        # timeouts courts : si MongoDB est absent en dev, l'app ne bloque pas
        self.client = MongoClient(
            app.config["MONGO_URI"],
            serverSelectionTimeoutMS=800,
            connectTimeoutMS=800,
            socketTimeoutMS=800,
        )
        self.db = self.client[app.config["MONGO_DB"]]


mongo = Mongo()
