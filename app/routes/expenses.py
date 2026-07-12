"""Module Charges & Dépenses — CRUD dépenses + catégories.

Sécurité (OWASP contrôle d'accès) :
- @login_required + @subscription_required sur toutes les routes.
- Chaque dépense appartient à un gérant : filtrage systématique par
  user_id = current_user.id (jamais les données d'un autre gérant).
- CSRF assuré globalement par Flask-WTF (CSRFProtect) sur tous les POST.
- Validation des entrées côté serveur (montant numérique > 0, libellé requis).
- Actions importantes journalisées via log_action (MongoDB).
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

# Catégories créées automatiquement au premier accès si la table est vide.
DEFAULT_CATEGORIES = [
    "Carburant", "Fournitures", "Loyer", "Salaires", "Transport", "Autre",
]


# ─────────────────────────── Helpers ───────────────────────────

def _ensure_default_categories():
    """Crée les catégories par défaut du gérant courant si aucune n'existe encore.

    Les catégories sont propres à chaque gérant (isolation OWASP).
    """
    if ExpenseCategory.query.filter_by(user_id=current_user.id).count() == 0:
        for name in DEFAULT_CATEGORIES:
            db.session.add(ExpenseCategory(name=name, user_id=current_user.id))
        db.session.commit()


def _owned_category_or_404(category_id):
    """Retourne une catégorie du gérant courant, sinon 404."""
    cat = ExpenseCategory.query.filter_by(
        id=category_id, user_id=current_user.id
    ).first()
    if cat is None:
        abort(404)
    return cat


def _parse_amount(raw):
    """Valide un montant : numérique et strictement positif. Renvoie Decimal ou None."""
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
    """Parse une date YYYY-MM-DD ; renvoie la date du jour si vide/invalide."""
    if raw:
        try:
            return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return date.today()


def _owned_expense_or_404(expense_id):
    """Récupère une dépense appartenant au gérant courant, sinon 404."""
    expense = Expense.query.filter_by(
        id=expense_id, user_id=current_user.id
    ).first()
    if expense is None:
        abort(404)
    return expense


# ─────────────────────────── Dépenses ───────────────────────────

@expenses_bp.route("/")
@login_required
@subscription_required
def index():
    """Liste des dépenses du gérant, filtrable par catégorie et par mois."""
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
            month = None  # filtre ignoré si format invalide

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
    """Formulaire de création d'une dépense."""
    _ensure_default_categories()
    categories = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
    return render_template(
        "expenses/form.html",
        expense=None,
        categories=categories,
        today=date.today().isoformat(),
    )


def _save_receipt(jpeg_bytes, user_id):
    """Enregistre le reçu (JPEG) sous /static/uploads/recus/, retourne le chemin relatif."""
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
        current_app.logger.warning("Échec sauvegarde reçu", exc_info=True)
        return None


@expenses_bp.route("/scan")
@login_required
@subscription_required
def scan():
    """Page d'import d'un reçu/facture de charge à analyser."""
    return render_template("expenses/scan.html")


@expenses_bp.route("/scan", methods=["POST"])
@login_required
@subscription_required
def scan_upload():
    """Reçoit le reçu (image/PDF), le lit via Claude Vision, pré-remplit le formulaire."""
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

    # Sauvegarde du reçu (image normalisée) pour le rattacher à la dépense.
    receipt_ref = _save_receipt(result["image"], current_user.id)

    fields = result["fields"]
    _ensure_default_categories()
    categories = ExpenseCategory.query.filter_by(
        user_id=current_user.id).order_by(ExpenseCategory.name).all()

    # Matcher la catégorie proposée par l'IA avec celles du gérant.
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
    """Enregistre une nouvelle dépense (validation serveur)."""
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

    # Reçu scanné : ne garder que les chemins qu'on a nous-mêmes générés (anti-injection).
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
    """Formulaire d'édition d'une dépense du gérant."""
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
    """Met à jour une dépense existante (validation serveur)."""
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
    """Supprime une dépense du gérant courant."""
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


# ─────────────────────────── Catégories ───────────────────────────

@expenses_bp.route("/categories")
@login_required
@subscription_required
def categories():
    """Gestion des catégories de dépenses (partagées entre gérants)."""
    _ensure_default_categories()
    items = ExpenseCategory.query.filter_by(user_id=current_user.id).order_by(ExpenseCategory.name).all()
    # nb de dépenses du gérant courant par catégorie (pour info suppression)
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
    """Crée une nouvelle catégorie."""
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
    """Supprime une catégorie du gérant (interdit si elle a des dépenses)."""
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
