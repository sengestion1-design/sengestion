"""Quote / Invoice / line items / Payment models (MySQL) - Quotes & Invoices module."""
from datetime import datetime

from app.extensions import db


class Quote(db.Model):
    __tablename__ = "quotes"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    number = db.Column(db.String(30), unique=True)               # Quote number
    quote_date = db.Column(db.Date)
    status = db.Column(
        db.Enum("draft", "sent", "accepted", "refused"),
        default="draft", nullable=False,
    )
    amount_excl_tax = db.Column(db.Numeric(12, 2), default=0)     # amount excl. tax
    amount_incl_tax = db.Column(db.Numeric(12, 2), default=0)     # amount incl. tax
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"))  # if converted
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("QuoteItem", backref="quote", lazy=True,
                            cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Quote {self.number}>"


class QuoteItem(db.Model):
    __tablename__ = "quote_items"

    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)
    description = db.Column(db.String(255), nullable=False)       # line label
    quantity = db.Column(db.Numeric(10, 2), default=1)
    unit_price = db.Column(db.Numeric(12, 2), default=0)
    amount = db.Column(db.Numeric(12, 2), default=0)


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"))  # source quote
    number = db.Column(db.String(30), unique=True)               # Invoice number
    invoice_date = db.Column(db.Date)
    status = db.Column(
        db.Enum("unpaid", "partial", "paid"),
        default="unpaid", nullable=False,
    )
    amount_excl_tax = db.Column(db.Numeric(12, 2), default=0)
    amount_incl_tax = db.Column(db.Numeric(12, 2), default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("InvoiceItem", backref="invoice", lazy=True,
                            cascade="all, delete-orphan")
    payments = db.relationship("Payment", backref="invoice", lazy=True)

    def __repr__(self):
        return f"<Invoice {self.number}>"


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), default=1)
    unit_price = db.Column(db.Numeric(12, 2), default=0)
    amount = db.Column(db.Numeric(12, 2), default=0)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.Date)
    method = db.Column(
        db.Enum("cash", "transfer", "wave", "orange_money", "check"),
        default="cash",
    )
