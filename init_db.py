"""Initialisation de la base : crée les tables MySQL + un compte admin de test.

Usage : python init_db.py
"""
from app import create_app
from app.extensions import db
from app.models.user import User

app = create_app("development")

with app.app_context():
    db.create_all()
    print("✅ Tables MySQL créées.")

    if not User.query.filter_by(email="admin@sengestion.sn").first():
        admin = User(name="Administrateur", email="admin@sengestion.sn",
                     role="admin", status="active", email_verified=True)
        admin.set_password("admin1234")   # à changer !
        db.session.add(admin)
        db.session.commit()
        print("✅ Compte admin créé : admin@sengestion.sn / admin1234")
    else:
        print("ℹ️  Le compte admin existe déjà.")
