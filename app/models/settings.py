"""CompanySettings model (MySQL) — per-manager company & email settings.

One row per user (1-1 relationship with User). Feeds:
- the header and footer of PDFs (quotes/invoices): name, address, logo, stamp, signature;
- the signature of emails sent to contacts.

Image paths (logo, signature, stamp) are relative to /static
(e.g. "uploads/settings/u1-logo-xxxx.png").
"""
from datetime import datetime

from app.extensions import db


class CompanySettings(db.Model):
    __tablename__ = "company_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        unique=True, nullable=False)          # 1-1 with the manager

    # --- Company identity ---
    company_name = db.Column(db.String(150))
    address = db.Column(db.Text)
    phone = db.Column(db.String(40))
    email = db.Column(db.String(150))
    website = db.Column(db.String(150))
    ninea = db.Column(db.String(50))                          # SN tax identifier
    rccm = db.Column(db.String(50))                           # SN trade register
    rc = db.Column(db.String(50))                             # RC number
    legal_form = db.Column(db.String(50))                     # legal form (SARL, SA…)
    capital = db.Column(db.String(50))                        # share capital

    # --- Images (paths relative to /static) ---
    logo = db.Column(db.String(255))
    # Stamp bearing the signature (a single gesture: one signs on the stamp).
    stamp = db.Column(db.String(255))

    # --- PDF notices & footer ---
    footer_note = db.Column(db.Text)                          # e.g. payment terms

    # --- Email settings ---
    email_sender_name = db.Column(db.String(120))             # name displayed when sending
    email_signature = db.Column(db.Text)                      # appended at the bottom of emails

    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    @classmethod
    def get_or_create(cls, user_id: int) -> "CompanySettings":
        """Return the manager's settings, creating an empty row if needed."""
        s = cls.query.filter_by(user_id=user_id).first()
        if s is None:
            s = cls(user_id=user_id)
            db.session.add(s)
            db.session.commit()
        return s

    def __repr__(self):
        return f"<CompanySettings user={self.user_id} {self.company_name}>"
