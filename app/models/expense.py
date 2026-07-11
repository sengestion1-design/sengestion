"""Expense / ExpenseCategory models (MySQL) — Expenses module."""
from datetime import datetime

from app.extensions import db


class ExpenseCategory(db.Model):
    __tablename__ = "expense_categories"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    # Propriétaire : chaque gérant a ses propres catégories (isolation OWASP).
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)             # Category name

    expenses = db.relationship("Expense", backref="category", lazy=True)

    def __repr__(self):
        return f"<ExpenseCategory {self.name}>"


class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("expense_categories.id"))
    label = db.Column(db.String(255), nullable=False)            # Expense label
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    expense_date = db.Column(db.Date)
    # receipt file reference (file + AI data stored in MongoDB)
    receipt_ref = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Expense {self.label} {self.amount}>"
