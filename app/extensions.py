"""Shared application extensions (initialized in the factory)."""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_mail import Mail
from pymongo import MongoClient

# --- MySQL / relational ---
db = SQLAlchemy()
migrate = Migrate()

# --- Auth & security ---
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = None
csrf = CSRFProtect()

# --- Email (Gmail SMTP) ---
mail = Mail()


class Mongo:
    """Small wrapper around PyMongo, initialized in the factory.

    Usage: from app.extensions import mongo ; mongo.db.logs.insert_one(...)
    """

    client: MongoClient = None
    db = None

    def init_app(self, app):
        # short timeouts: if MongoDB is absent in dev, the app does not block
        self.client = MongoClient(
            app.config["MONGO_URI"],
            serverSelectionTimeoutMS=800,
            connectTimeoutMS=800,
            socketTimeoutMS=800,
        )
        self.db = self.client[app.config["MONGO_DB"]]


mongo = Mongo()
