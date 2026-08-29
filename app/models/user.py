"""User model (MySQL / relational).

Security:
- password stored hashed (werkzeug), never in clear text
- Flask-Login integration (UserMixin)

SaaS subscription:
- role admin (manages SenGestion) or manager (subscribing customer)
- 15-day free trial on sign-up, then yearly subscription validated by the admin
- statuses: trial -> active -> expired (+ suspended, set manually)
"""
import secrets
from datetime import datetime, date, timedelta

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db, login_manager

TRIAL_DAYS = 15
SUBSCRIPTION_DAYS = 365
PLAN_DAYS = {"monthly": 30, "annual": 365}
CODE_VALIDITY_MINUTES = 15


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)                 # Unique identifier
    name = db.Column(db.String(100), nullable=False)             # Full name
    company = db.Column(db.String(150))                          # Manager's company
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)    # Hashed password

    role = db.Column(db.Enum("admin", "manager"), default="manager", nullable=False)

    # --- email verification (6-digit code) ---
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    verification_code = db.Column(db.String(6))          # 6-digit code
    verification_expires = db.Column(db.DateTime)        # code validity limit

    # --- email change (pending confirmation on the NEW address) ---
    pending_email = db.Column(db.String(150))             # new address, not active until confirmed
    pending_email_code = db.Column(db.String(6))
    pending_email_expires = db.Column(db.DateTime)

    # --- subscription ---
    status = db.Column(
        db.Enum("trial", "active", "expired", "suspended"),
        default="trial", nullable=False,
    )
    trial_start = db.Column(db.Date, default=date.today)         # trial start date
    trial_end = db.Column(db.Date)                               # trial_start + 15 days
    subscription_start = db.Column(db.Date)                      # validated by admin
    subscription_end = db.Column(db.Date)                        # start + 1 year
    validated_by = db.Column(db.Integer, db.ForeignKey("users.id"))  # admin who validated

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ------------------------------------------------------------------
    # Password security
    # ------------------------------------------------------------------
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    # ------------------------------------------------------------------
    # Email verification (6-digit code)
    # ------------------------------------------------------------------
    def generate_verification_code(self) -> str:
        """Generate a fresh 6-digit code valid for 15 minutes."""
        code = f"{secrets.randbelow(1_000_000):06d}"   # 000000..999999
        self.verification_code = code
        self.verification_expires = datetime.utcnow() + timedelta(minutes=CODE_VALIDITY_MINUTES)
        return code

    def check_verification_code(self, code: str) -> bool:
        """Return True if the code is correct and not expired."""
        if not self.verification_code or not self.verification_expires:
            return False
        if datetime.utcnow() > self.verification_expires:
            return False
        return secrets.compare_digest(self.verification_code, (code or "").strip())

    def mark_email_verified(self) -> None:
        self.email_verified = True
        self.verification_code = None
        self.verification_expires = None

    # ------------------------------------------------------------------
    # Email change (confirmation required on the new address before it
    # becomes active - the current email stays valid until then).
    # ------------------------------------------------------------------
    def start_email_change(self, new_email: str) -> str:
        """Stage a new email and return a fresh 6-digit code (valid 15 min)."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.pending_email = new_email
        self.pending_email_code = code
        self.pending_email_expires = datetime.utcnow() + timedelta(minutes=CODE_VALIDITY_MINUTES)
        return code

    def check_pending_email_code(self, code: str) -> bool:
        if not self.pending_email or not self.pending_email_code or not self.pending_email_expires:
            return False
        if datetime.utcnow() > self.pending_email_expires:
            return False
        return secrets.compare_digest(self.pending_email_code, (code or "").strip())

    def confirm_email_change(self) -> None:
        self.email = self.pending_email
        self.pending_email = None
        self.pending_email_code = None
        self.pending_email_expires = None

    def cancel_email_change(self) -> None:
        self.pending_email = None
        self.pending_email_code = None
        self.pending_email_expires = None

    # ------------------------------------------------------------------
    # Subscription business logic
    # ------------------------------------------------------------------
    def start_trial(self) -> None:
        """Start the 15-day free trial (on sign-up)."""
        self.status = "trial"
        self.trial_start = date.today()
        self.trial_end = date.today() + timedelta(days=TRIAL_DAYS)

    def validate_subscription(self, admin_id: int, plan: str = "annual") -> None:
        """Admin validates the subscription (monthly = 30 days, annual = 365 days)."""
        days = PLAN_DAYS.get(plan, SUBSCRIPTION_DAYS)
        self.status = "active"
        self.subscription_start = date.today()
        self.subscription_end = date.today() + timedelta(days=days)
        self.validated_by = admin_id

    def suspend(self) -> None:
        self.status = "suspended"

    @property
    def access_end_date(self):
        """Effective access end date (trial or subscription)."""
        if self.status == "active":
            return self.subscription_end
        return self.trial_end

    @property
    def days_left(self) -> int:
        """Remaining access days (0 if expired)."""
        end = self.access_end_date
        if not end:
            return 0
        return max(0, (end - date.today()).days)

    @property
    def access_allowed(self) -> bool:
        """True if the manager can use the application."""
        if self.role == "admin":
            return True
        if self.status == "suspended":
            return False
        return self.status in ("trial", "active") and self.days_left > 0

    def refresh_status(self) -> None:
        """Move the account to 'expired' if the end date has passed."""
        if self.role == "admin":
            return
        if self.status in ("trial", "active") and self.days_left == 0:
            self.status = "expired"

    def __repr__(self):
        return f"<User {self.email} ({self.role}/{self.status})>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
