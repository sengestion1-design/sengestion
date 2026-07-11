"""Application factory SenGestion.

Architecture MVC :
- models/   : entités MySQL (SQLAlchemy) — CP5
- routes/   : contrôleurs (blueprints) — CP7
- services/ : logique métier & accès NoSQL — CP6/CP7
"""
from flask import Flask

from config.settings import config
from app.extensions import db, migrate, login_manager, csrf, mongo, mail


def create_app(env="default"):
    app = Flask(__name__)
    app.config.from_object(config[env])

    # --- Bases de données ---
    db.init_app(app)                 # MySQL (relationnel)
    migrate.init_app(app, db)
    mongo.init_app(app)              # MongoDB (NoSQL)

    # --- Sécurité & email ---
    login_manager.init_app(app)
    csrf.init_app(app)
    mail.init_app(app)               # Gmail SMTP

    # --- Models (imported so Flask-Migrate can see them) ---
    from app.models import user, customer, quote, expense, message  # noqa: F401

    # --- Blueprints (routes) ---
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.admin import admin_bp
    from app.routes.subscription import subscription_bp
    from app.routes.customers import customers_bp, contacts_bp
    from app.routes.quotes import quotes_bp, invoices_bp
    from app.routes.expenses import expenses_bp
    from app.routes.messages import messages_bp
    from app.routes.settings import settings_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(subscription_bp)
    # modules métier
    app.register_blueprint(customers_bp)
    app.register_blueprint(contacts_bp)
    app.register_blueprint(quotes_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(messages_bp)
    app.register_blueprint(settings_bp)

    return app
