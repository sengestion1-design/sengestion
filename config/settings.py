"""SenGestion application configuration.

Two databases:
- MySQL (relational) : business core - competency CP5 + SQL part of CP6.
- MongoDB (NoSQL)    : activity logs & AI data - NoSQL part of CP6.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")

    # --- MySQL (SQLAlchemy) ---
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{os.getenv('DB_USER', 'root')}:"
        f"{os.getenv('DB_PASSWORD', '')}@"
        f"{os.getenv('DB_HOST', 'localhost')}:"
        f"{os.getenv('DB_PORT', '3306')}/"
        f"{os.getenv('DB_NAME', 'sengestion')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- MongoDB (PyMongo) ---
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    MONGO_DB = os.getenv("MONGO_DB", "sengestion_nosql")

    # --- Security / uploads ---
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 24  # 24h - avoids expiry if the page stays open a long time (demo)

    # --- Email (Gmail SMTP) ---
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", 587))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "sengestion1@gmail.com")

    # --- AI (Claude Vision - business-card / receipt scan) ---
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    # Vision model used for extraction (overridable via .env)
    ANTHROPIC_VISION_MODEL = os.getenv("ANTHROPIC_VISION_MODEL", "claude-sonnet-5")


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


class DockerConfig(ProductionConfig):
    """Container execution for the local demo: debug disabled,
    but non-Secure cookies since the app is served over HTTP on localhost."""
    SESSION_COOKIE_SECURE = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "docker": DockerConfig,
    "default": DevelopmentConfig,
}
