"""PaymentProof model (MySQL) - subscription payment proof submitted by a manager."""
from datetime import datetime

from app.extensions import db


class PaymentProof(db.Model):
    __tablename__ = "payment_proofs"

    id = db.Column(db.Integer, primary_key=True)                  # Unique identifier
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    plan = db.Column(db.Enum("monthly", "annual"), nullable=False)  # subscription plan chosen
    method = db.Column(db.Enum("orange_money", "wave"), nullable=False)  # transfer method used
    transaction_number = db.Column(db.String(50), nullable=False)  # mobile money transfer reference
    image_path = db.Column(db.String(255), nullable=False)         # proof photo (relative to static/)
    status = db.Column(
        db.Enum("pending", "approved", "rejected"),
        default="pending", nullable=False,
    )
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))  # admin who reviewed it
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id], backref="payment_proofs")

    def approve(self, admin_id: int) -> None:
        self.status = "approved"
        self.reviewed_by = admin_id
        self.reviewed_at = datetime.utcnow()

    def reject(self, admin_id: int) -> None:
        self.status = "rejected"
        self.reviewed_by = admin_id
        self.reviewed_at = datetime.utcnow()

    def __repr__(self):
        return f"<PaymentProof user={self.user_id} plan={self.plan} status={self.status}>"
