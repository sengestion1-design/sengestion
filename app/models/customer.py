"""Customer and Contact models (MySQL) — Contacts & Customers module."""
from datetime import datetime

from app.extensions import db


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)  # owner
    name = db.Column(db.String(100), nullable=False)             # Customer name
    company = db.Column(db.String(150))                          # Company name
    email = db.Column(db.String(150))
    phone = db.Column(db.String(20))
    address = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    quotes = db.relationship("Quote", backref="customer", lazy=True)
    invoices = db.relationship("Invoice", backref="customer", lazy=True)

    def __repr__(self):
        return f"<Customer {self.name}>"


class Contact(db.Model):
    __tablename__ = "contacts"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)  # owner
    name = db.Column(db.String(100), nullable=False)             # Contact name
    first_name = db.Column(db.String(100))
    company = db.Column(db.String(150))
    role = db.Column(db.String(100))                             # job title
    email = db.Column(db.String(150))
    phone = db.Column(db.String(20))
    # kind : "contact" = address book (no active outreach);
    #        "prospect" = sales lead tracked in the pipeline.
    kind = db.Column(
        db.Enum("contact", "prospect"),
        default="contact", nullable=False,
    )
    status = db.Column(                                          # prospect pipeline
        db.Enum("new", "in_progress", "won", "lost"),
        default="new", nullable=False,
    )
    source = db.Column(db.Enum("business_card_scan", "manual"), default="manual")
    # Relative path of the scanned card photo (e.g. "uploads/cartes/xxx.jpg"), under /static.
    card_image = db.Column(db.String(255))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))  # if converted
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def is_converted(self):
        return self.customer_id is not None

    def __repr__(self):
        return f"<Contact {self.name} ({self.kind})>"
