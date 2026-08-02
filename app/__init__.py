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
    from app.routes.reports import reports_bp
    from app.routes.messages import messages_bp
    from app.routes.settings import settings_bp
    from app.routes.legal import legal_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(legal_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(subscription_bp)
    # modules métier
    app.register_blueprint(customers_bp)
    app.register_blueprint(contacts_bp)
    app.register_blueprint(quotes_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(messages_bp)
    app.register_blueprint(settings_bp)

    # Précharge le modèle Whisper (transcription vocale) en arrière-plan,
    # pour que la première dictée soit rapide (pas d'attente de chargement).
    try:
        from app.services.transcription_service import preload_model_async
        preload_model_async()
    except Exception:
        pass

    # Cache-busting : ajoute la date de modif du fichier à l'URL statique.
    # Ainsi tout changement CSS/JS force le navigateur à recharger (fini le cache).
    import os
    from flask import url_for

    @app.context_processor
    def _static_versioning():
        def static_v(filename):
            path = os.path.join(app.static_folder, filename)
            try:
                ver = int(os.path.getmtime(path))
            except OSError:
                ver = 0
            return url_for("static", filename=filename, v=ver)
        return {"static_v": static_v}

    # Compteurs du menu (factures impayées, relances en attente).
    @app.context_processor
    def _sidebar_counters():
        from flask_login import current_user
        if not current_user.is_authenticated or current_user.role != "manager":
            return {"unpaid_invoices_count": 0, "pending_messages_count": 0}
        from app.models.quote import Invoice
        from app.models.message import Message
        unpaid = Invoice.query.filter_by(
            user_id=current_user.id
        ).filter(Invoice.status.in_(["unpaid", "partial"])).count()
        pending = Message.query.filter_by(user_id=current_user.id, status="draft").count()
        return {"unpaid_invoices_count": unpaid, "pending_messages_count": pending}

    return app
