"""Charges & Expenses module - expenses + categories CRUD.

Security (OWASP access control):
- @login_required + @subscription_required on all routes.
- Every expense belongs to a manager: systematic filtering by
  user_id = current_user.id (never another manager's data).
- CSRF handled globally by Flask-WTF (CSRFProtect) on all POSTs.
- Server-side input validation (numeric amount > 0, label required).
- Important actions logged via log_action (MongoDB).
"""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)
from flask_login import login_required, current_user
from sqlalchemy import extract

from app.extensions import db
from app.models.expense import Expense, ExpenseCategory
from app.services.activity_log_service import log_action
from app.utils.access import subscription_required

expenses_bp = Blueprint("expenses", __name__, url_prefix="/expenses")

# Categories created automatically on first access if the table is empty.
DEFAULT_CATEGORIES = [
    "Carburant", "Fournitures", "Loyer", "Salaires", "Transport", "Autre",
]


# --------------------------- Helpers ---------------------------

def _ensure_default_categories():
    """Create the current manager's default categories if none exist yet.

    Categories are specific to each manager (OWASP isolation).
    """
    if ExpenseCategory.query.filter_by(user_id=current_user.id).count() == 0:
        for name in DEFAULT_CATEGORIES:
            db.session.add(ExpenseCategory(name=name, user_id=current_user.id))
        db.session.commit()


def _owned_category_or_404(category_id):
    """Return a category owned by the current manager, or 404."""
    cat = ExpenseCategory.query.filter_by(
        id=category_id, user_id=current_user.id
    ).first()
    if cat is None:
        abort(404)
    return cat


def _parse_amount(raw):
    """Validate an amount: numeric and strictly positive. Returns Decimal or None."""
    if raw is None:
        return None
    raw = str(raw).strip().replace(" ", "").replace(",", ".")
    if raw == "":
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_date(raw):
    """Parse a YYYY-MM-DD date; returns today's date if empty/invalid."""
    if raw:
        try:
            return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return date.today()


def _owned_expense_or_404(expense_id):
    """Fetch an expense owned by the current manager, or 404."""
    expense = Expense.query.filter_by(
        id=expense_id, user_id=current_user.id
    ).first()
    if expense is None:
        abort(404)
    return expense


# --------------------------- Expenses ---------------------------

@expenses_bp.route("/")
@login_required
@subscription_required
def index():
    """List the manager's expenses, filterable by category and by month."""
    _ensure_default_categories()

    category_id = request.args.get("category", type=int)
    month = request.args.get("month", type=str)  # format 'YYYY-MM'

    query = Expense.query.filter_by(user_id=current_user.id)

    if category_id:
        query = query.filter(Expense.category_id == category_id)

    if month:
        try:
            year_s, month_s = month.split("-")
            query = query.filter(
                extract("year", Expense.expense_date) == int(year_s),
                extract("month", Expense.expense_date) == int(month_s),
            )
        except (ValueError, AttributeError):
            month = None  # filter ignored if the format is invalid

    expenses = query.order_by(
        Expense.expense_date.desc(), Expense.id.desc()
    ).all()

    total = sum((e.amount or Decimal("0")) for e in expenses)
    categories = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()

    return render_template(
        "expenses/index.html",
        expenses=expenses,
        categories=categories,
        total=total,
        count=len(expenses),
        selected_category=category_id,
        selected_month=month,
    )


@expenses_bp.route("/new")
@login_required
@subscription_required
def new():
    """Expense creation form."""
    _ensure_default_categories()
    categories = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
    return render_template(
        "expenses/form.html",
        expense=None,
        categories=categories,
        today=date.today().isoformat(),
    )


def _save_receipt(jpeg_bytes, user_id):
    """Save the receipt (JPEG) under /static/uploads/recus/, return the relative path."""
    import os
    import uuid
    from flask import current_app
    try:
        rel_dir = os.path.join("uploads", "recus")
        abs_dir = os.path.join(current_app.static_folder, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        fname = f"u{user_id}-{uuid.uuid4().hex[:12]}.jpg"
        with open(os.path.join(abs_dir, fname), "wb") as fh:
            fh.write(jpeg_bytes)
        return f"{rel_dir}/{fname}"
    except Exception:
        current_app.logger.warning("Failed to save receipt", exc_info=True)
        return None


@expenses_bp.route("/scan")
@login_required
@subscription_required
def scan():
    """Upload page for an expense receipt/invoice to analyze."""
    return render_template("expenses/scan.html")


@expenses_bp.route("/scan", methods=["POST"])
@login_required
@subscription_required
def scan_upload():
    """Receive the receipt (image/PDF), read it via Claude Vision, pre-fill the form."""
    from app.services.receipt_scan_service import scan_receipt

    file = request.files.get("receipt")
    if file is None or not file.filename:
        flash("Veuillez sélectionner une image ou un PDF de reçu.", "danger")
        return redirect(url_for("expenses.scan"))

    raw = file.read()
    result = scan_receipt(raw, filename=file.filename, content_type=file.mimetype)
    log_action(current_user.id, "scan_receipt",
               {"ok": result.get("ok"), "filename": file.filename})

    if not result.get("ok"):
        flash(result.get("error", "Le reçu n'a pas pu être analysé."), "danger")
        return redirect(url_for("expenses.scan"))

    # Save the receipt (normalized image) to attach it to the expense.
    receipt_ref = _save_receipt(result["image"], current_user.id)

    fields = result["fields"]
    _ensure_default_categories()
    categories = ExpenseCategory.query.filter_by(
        user_id=current_user.id).order_by(ExpenseCategory.name).all()

    # Match the category suggested by the AI with the manager's categories.
    matched_cat = None
    wanted = (fields.get("category") or "").strip().lower()
    for c in categories:
        if c.name.strip().lower() == wanted:
            matched_cat = c.id
            break

    from werkzeug.datastructures import MultiDict
    form = MultiDict()
    form["label"] = fields.get("label", "")
    if fields.get("amount"):
        form["amount"] = str(fields["amount"])
    form["expense_date"] = fields.get("date") or date.today().isoformat()
    if matched_cat:
        form["category_id"] = str(matched_cat)
    if receipt_ref:
        form["receipt_ref"] = receipt_ref

    flash("Reçu analysé : vérifiez et complétez avant d'enregistrer.", "success")
    return render_template(
        "expenses/form.html",
        expense=None, categories=categories,
        today=date.today().isoformat(),
        form=form, from_scan=True, receipt_ref=receipt_ref,
    )


@expenses_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def create():
    """Save a new expense (server-side validation)."""
    label = (request.form.get("label") or "").strip()
    amount = _parse_amount(request.form.get("amount"))
    expense_date = _parse_date(request.form.get("expense_date"))
    category_id = request.form.get("category_id", type=int)

    errors = []
    if not label:
        errors.append("Le libellé est obligatoire.")
    if amount is None:
        errors.append("Le montant doit être un nombre supérieur à 0.")
    if expense_date is None:
        errors.append("La date est invalide.")
    if category_id and not ExpenseCategory.query.filter_by(id=category_id, user_id=current_user.id).first():
        errors.append("La catégorie sélectionnée est introuvable.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        categories = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
        return render_template(
            "expenses/form.html",
            expense=None,
            categories=categories,
            today=date.today().isoformat(),
            form=request.form,
        ), 400

    # Scanned receipt: only keep paths we generated ourselves (anti-injection).
    receipt_ref = request.form.get("receipt_ref") or None
    if receipt_ref and (not receipt_ref.startswith("uploads/recus/") or ".." in receipt_ref):
        receipt_ref = None

    expense = Expense(
        user_id=current_user.id,
        category_id=category_id or None,
        label=label,
        amount=amount,
        expense_date=expense_date,
        receipt_ref=receipt_ref,
    )
    db.session.add(expense)
    db.session.commit()

    log_action(current_user.id, "create_expense", {
        "expense_id": expense.id,
        "label": label,
        "amount": str(amount),
    })
    flash("Dépense enregistrée avec succès.", "success")
    return redirect(url_for("expenses.index"))


@expenses_bp.route("/<int:expense_id>/edit")
@login_required
@subscription_required
def edit(expense_id):
    """Edit form for one of the manager's expenses."""
    expense = _owned_expense_or_404(expense_id)
    categories = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
    return render_template(
        "expenses/form.html",
        expense=expense,
        categories=categories,
        today=date.today().isoformat(),
    )


@expenses_bp.route("/<int:expense_id>", methods=["POST"])
@login_required
@subscription_required
def update(expense_id):
    """Update an existing expense (server-side validation)."""
    expense = _owned_expense_or_404(expense_id)

    label = (request.form.get("label") or "").strip()
    amount = _parse_amount(request.form.get("amount"))
    expense_date = _parse_date(request.form.get("expense_date"))
    category_id = request.form.get("category_id", type=int)

    errors = []
    if not label:
        errors.append("Le libellé est obligatoire.")
    if amount is None:
        errors.append("Le montant doit être un nombre supérieur à 0.")
    if expense_date is None:
        errors.append("La date est invalide.")
    if category_id and not ExpenseCategory.query.filter_by(id=category_id, user_id=current_user.id).first():
        errors.append("La catégorie sélectionnée est introuvable.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        categories = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
        return render_template(
            "expenses/form.html",
            expense=expense,
            categories=categories,
            today=date.today().isoformat(),
            form=request.form,
        ), 400

    expense.label = label
    expense.amount = amount
    expense.expense_date = expense_date
    expense.category_id = category_id or None
    db.session.commit()

    log_action(current_user.id, "update_expense", {
        "expense_id": expense.id,
        "label": label,
        "amount": str(amount),
    })
    flash("Dépense mise à jour.", "success")
    return redirect(url_for("expenses.index"))


@expenses_bp.route("/<int:expense_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete(expense_id):
    """Delete an expense owned by the current manager."""
    expense = _owned_expense_or_404(expense_id)
    label = expense.label
    db.session.delete(expense)
    db.session.commit()

    log_action(current_user.id, "delete_expense", {
        "expense_id": expense_id,
        "label": label,
    })
    flash("Dépense supprimée.", "success")
    return redirect(url_for("expenses.index"))


# --------------------------- Categories ---------------------------

@expenses_bp.route("/categories")
@login_required
@subscription_required
def categories():
    """Expense category management (each manager owns their own categories)."""
    _ensure_default_categories()
    items = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
    # current manager's expense count per category (deletion info)
    usage = {}
    for cat in items:
        usage[cat.id] = Expense.query.filter_by(
            user_id=current_user.id, category_id=cat.id
        ).count()
    return render_template(
        "expenses/categories.html",
        categories=items,
        usage=usage,
    )


@expenses_bp.route("/categories", methods=["POST"])
@login_required
@subscription_required
def create_category():
    """Create a new category."""
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Le nom de la catégorie est obligatoire.", "danger")
        return redirect(url_for("expenses.categories"))

    existing = ExpenseCategory.query.filter(
        ExpenseCategory.user_id == current_user.id,
        db.func.lower(ExpenseCategory.name) == name.lower()
    ).first()
    if existing:
        flash("Cette catégorie existe déjà.", "warning")
        return redirect(url_for("expenses.categories"))

    category = ExpenseCategory(name=name, user_id=current_user.id)
    db.session.add(category)
    db.session.commit()

    log_action(current_user.id, "create_expense_category", {
        "category_id": category.id,
        "name": name,
    })
    flash("Catégorie ajoutée.", "success")
    return redirect(url_for("expenses.categories"))


@expenses_bp.route("/categories/<int:category_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete_category(category_id):
    """Delete one of the manager's categories (forbidden if it has expenses)."""
    category = _owned_category_or_404(category_id)

    used = Expense.query.filter_by(
        user_id=current_user.id, category_id=category_id
    ).count()
    if used > 0:
        flash(
            "Impossible de supprimer : cette catégorie contient des dépenses.",
            "warning",
        )
        return redirect(url_for("expenses.categories"))

    name = category.name
    db.session.delete(category)
    db.session.commit()

    log_action(current_user.id, "delete_expense_category", {
        "category_id": category_id,
        "name": name,
    })
    flash("Catégorie supprimée.", "success")
    return redirect(url_for("expenses.categories"))
