"""Message model (MySQL) — Communication / Follow-up module."""
from datetime import datetime

from app.extensions import db


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))
    channel = db.Column(db.Enum("email"), default="email")
    subject = db.Column(db.String(255))
    body = db.Column(db.Text)
    status = db.Column(
        db.Enum("draft", "sent", "failed"),
        default="draft", nullable=False,
    )
    sent_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Message {self.subject}>"
