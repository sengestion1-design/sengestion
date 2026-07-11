"""CompanySettings model (MySQL) — paramètres entreprise & email par gérant.

Une ligne par utilisateur (relation 1-1 avec User). Alimente :
- l'en-tête et le pied des PDF (devis/factures) : nom, adresse, logo, cachet, signature ;
- la signature des emails envoyés aux contacts.

Les chemins d'images (logo, signature, cachet) sont relatifs à /static
(ex. "uploads/settings/u1-logo-xxxx.png").
"""
from datetime import datetime

from app.extensions import db


class CompanySettings(db.Model):
    __tablename__ = "company_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        unique=True, nullable=False)          # 1-1 avec le gérant

    # --- Identité entreprise ---
    company_name = db.Column(db.String(150))
    address = db.Column(db.Text)
    phone = db.Column(db.String(40))
    email = db.Column(db.String(150))
    website = db.Column(db.String(150))
    ninea = db.Column(db.String(50))                          # identifiant fiscal SN
    rccm = db.Column(db.String(50))                           # registre du commerce SN

    # --- Images (chemins relatifs à /static) ---
    logo = db.Column(db.String(255))
    signature = db.Column(db.String(255))
    stamp = db.Column(db.String(255))                         # cachet

    # --- Mentions & pied de page PDF ---
    footer_note = db.Column(db.Text)                          # ex. conditions de paiement

    # --- Réglages email ---
    email_sender_name = db.Column(db.String(120))             # nom affiché à l'envoi
    email_signature = db.Column(db.Text)                      # ajoutée en bas des emails

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    @classmethod
    def get_or_create(cls, user_id: int) -> "CompanySettings":
        """Retourne les paramètres du gérant, en créant une ligne vide au besoin."""
        s = cls.query.filter_by(user_id=user_id).first()
        if s is None:
            s = cls(user_id=user_id)
            db.session.add(s)
            db.session.commit()
        return s

    def __repr__(self):
        return f"<CompanySettings user={self.user_id} {self.company_name}>"
