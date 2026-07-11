"""Module Devis & Factures — CRUD complet, lignes, calculs, PDF, paiements.

Deux blueprints :
  - quotes_bp   (/quotes)   : devis (création avec lignes, PDF, conversion en facture)
  - invoices_bp (/invoices) : factures (détail, PDF, paiements, suppression)

Sécurité (OWASP contrôle d'accès) :
- @login_required + @subscription_required sur TOUTES les routes.
- Chaque devis / facture appartient à un gérant : filtrage systématique par
  user_id = current_user.id (un gérant ne voit JAMAIS les données d'un autre).
- CSRF assuré globalement par Flask-WTF (CSRFProtect) — token rendu dans les forms.
- Validation des entrées côté serveur (client possédé, lignes valides, montants > 0).
- Actions importantes journalisées via log_action (MongoDB, best-effort).

Règle métier : TVA Sénégal 18 % (configurable via TAX_RATE), montants en FCFA.
"""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    Response,
)
from flask_login import login_required, current_user
from sqlalchemy import func

from app.extensions import db
from app.models.customer import Customer
from app.models.quote import Quote, QuoteItem, Invoice, InvoiceItem, Payment
from app.models.settings import CompanySettings
from app.services.activity_log_service import log_action
from app.utils.access import subscription_required

quotes_bp = Blueprint("quotes", __name__, url_prefix="/quotes")
invoices_bp = Blueprint("invoices", __name__, url_prefix="/invoices")

# Taux de TVA Sénégal (configurable). 0.18 = 18 %.
TAX_RATE = Decimal("0.18")

# Libellés + couleurs de badge par statut — CHARTE STRICTE (3 couleurs).
# Format : (libellé, fond, texte). L'or/jaune pâle = fond, texte toujours marine.
#   - positif (accepté / payé)  → jaune pâle
#   - en cours (envoyé/partielle)→ gris bleuté neutre (info-soft)
#   - négatif (refusé/impayé)    → gris neutre
#   - brouillon                  → gris fond + bordure
QUOTE_STATUSES = {
    "draft": ("Brouillon", "#F9FAFB", "#021A3D"),
    "sent": ("Envoyé", "#EEF1F6", "#021A3D"),
    "accepted": ("Accepté", "#E8E7A2", "#021A3D"),
    "refused": ("Refusé", "#F9FAFB", "#021A3D"),
}
INVOICE_STATUSES = {
    "unpaid": ("Impayée", "#F9FAFB", "#021A3D"),
    "partial": ("Partielle", "#EEF1F6", "#021A3D"),
    "paid": ("Payée", "#E8E7A2", "#021A3D"),
}
PAYMENT_METHODS = {
    "cash": "Espèces",
    "transfer": "Virement",
    "wave": "Wave",
    "orange_money": "Orange Money",
    "check": "Chèque",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _parse_decimal(raw, allow_zero=False):
    """Valide un nombre décimal. Renvoie Decimal(2) ou None si invalide."""
    if raw is None:
        return None
    raw = str(raw).strip().replace(" ", "").replace(",", ".")
    if raw == "":
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if value < 0:
        return None
    if value == 0 and not allow_zero:
        return None
    return value.quantize(Decimal("0.01"))


def _parse_date(raw):
    """Parse une date YYYY-MM-DD ; renvoie aujourd'hui si vide, None si invalide."""
    if raw:
        try:
            return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return date.today()


def _owned_customer_or_none(customer_id):
    """Client appartenant au gérant courant, sinon None."""
    if not customer_id:
        return None
    return Customer.query.filter_by(
        id=customer_id, user_id=current_user.id
    ).first()


def _owned_quote_or_404(quote_id):
    quote = Quote.query.filter_by(id=quote_id, user_id=current_user.id).first()
    if quote is None:
        abort(404)
    return quote


def _owned_invoice_or_404(invoice_id):
    invoice = Invoice.query.filter_by(
        id=invoice_id, user_id=current_user.id
    ).first()
    if invoice is None:
        abort(404)
    return invoice


def _next_number(prefix, model):
    """Génère un numéro lisible : {prefix}-{année}-{compteur zéro-paddé}.

    Le compteur est basé sur le nombre d'enregistrements du gérant courant
    pour l'année en cours + 1, avec garde anti-collision sur l'unicité.
    """
    year = date.today().year
    base = f"{prefix}-{year}-"
    count = model.query.filter(
        model.user_id == current_user.id,
        model.number.like(f"{base}%"),
    ).count()
    seq = count + 1
    # garde anti-collision (numéro unique global en base)
    while model.query.filter_by(number=f"{base}{seq:04d}").first() is not None:
        seq += 1
    return f"{base}{seq:04d}"


def _read_lines():
    """Lit les lignes du formulaire (designation[], quantity[], unit_price[]).

    Renvoie (items, errors) où items est une liste de dicts prêts à persister
    et errors une liste de messages. Ignore les lignes entièrement vides.
    """
    designations = request.form.getlist("designation[]")
    quantities = request.form.getlist("quantity[]")
    prices = request.form.getlist("unit_price[]")

    items, errors = [], []
    total_rows = max(len(designations), len(quantities), len(prices))
    for i in range(total_rows):
        desc = (designations[i] if i < len(designations) else "").strip()
        raw_qty = quantities[i] if i < len(quantities) else ""
        raw_price = prices[i] if i < len(prices) else ""

        # ligne entièrement vide → ignorée
        if not desc and not str(raw_qty).strip() and not str(raw_price).strip():
            continue

        qty = _parse_decimal(raw_qty)
        price = _parse_decimal(raw_price, allow_zero=True)

        if not desc:
            errors.append(f"Ligne {i + 1} : la désignation est obligatoire.")
        if len(desc) > 255:
            errors.append(f"Ligne {i + 1} : désignation trop longue (255 max).")
        if qty is None:
            errors.append(f"Ligne {i + 1} : quantité invalide (> 0 requise).")
        if price is None:
            errors.append(f"Ligne {i + 1} : prix unitaire invalide.")

        if desc and qty is not None and price is not None:
            items.append({
                "description": desc[:255],
                "quantity": qty,
                "unit_price": price,
                "amount": (qty * price).quantize(Decimal("0.01")),
            })

    if not items and not errors:
        errors.append("Ajoutez au moins une ligne au devis.")
    return items, errors


def _totals(items):
    """Calcule (HT, TTC) à partir d'une liste d'items (dicts avec 'amount')."""
    excl = sum((it["amount"] for it in items), Decimal("0")).quantize(Decimal("0.01"))
    incl = (excl * (Decimal("1") + TAX_RATE)).quantize(Decimal("0.01"))
    return excl, incl


def _paid_amount(invoice):
    """Somme des paiements enregistrés d'une facture (Decimal)."""
    total = db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.invoice_id == invoice.id
    ).scalar()
    return Decimal(str(total or 0)).quantize(Decimal("0.01"))


def _refresh_invoice_status(invoice):
    """Recalcule le statut d'une facture selon la somme des paiements."""
    paid = _paid_amount(invoice)
    total = Decimal(str(invoice.amount_incl_tax or 0))
    if paid <= 0:
        invoice.status = "unpaid"
    elif paid >= total:
        invoice.status = "paid"
    else:
        invoice.status = "partial"
    return paid


def _fmt(value):
    """Formate un montant FCFA sans décimales, séparateur espace."""
    try:
        return "{:,.0f}".format(Decimal(str(value or 0))).replace(",", " ")
    except (InvalidOperation, ValueError):
        return "0"


# ─────────────────────────────────────────────────────────────────────────────
#  DEVIS
# ─────────────────────────────────────────────────────────────────────────────
@quotes_bp.route("/")
@login_required
@subscription_required
def index():
    """Liste des devis du gérant (statut, client, TTC, date)."""
    status = request.args.get("status", type=str)
    query = Quote.query.filter_by(user_id=current_user.id)
    if status in QUOTE_STATUSES:
        query = query.filter(Quote.status == status)

    quotes = query.order_by(
        Quote.quote_date.desc(), Quote.id.desc()
    ).all()

    total_ttc = sum(
        (Decimal(str(q.amount_incl_tax or 0)) for q in quotes), Decimal("0")
    )
    return render_template(
        "quotes/index.html",
        quotes=quotes,
        statuses=QUOTE_STATUSES,
        selected_status=status if status in QUOTE_STATUSES else None,
        total_ttc=total_ttc,
        count=len(quotes),
        fmt=_fmt,
    )


@quotes_bp.route("/new")
@login_required
@subscription_required
def new():
    """Formulaire de création d'un devis."""
    customers = Customer.query.filter_by(
        user_id=current_user.id
    ).order_by(Customer.name).all()
    return render_template(
        "quotes/form.html",
        quote=None,
        customers=customers,
        tax_rate=TAX_RATE,
        today=date.today().isoformat(),
    )


@quotes_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def create():
    """Enregistre un nouveau devis avec ses lignes (validation serveur)."""
    customer_id = request.form.get("customer_id", type=int)
    quote_date = _parse_date(request.form.get("quote_date"))
    status = request.form.get("status", "draft")

    customer = _owned_customer_or_none(customer_id)
    items, errors = _read_lines()

    if customer is None:
        errors.insert(0, "Sélectionnez un client valide.")
    if quote_date is None:
        errors.append("La date du devis est invalide.")
    if status not in QUOTE_STATUSES:
        status = "draft"

    if errors:
        for msg in errors:
            flash(msg, "danger")
        customers = Customer.query.filter_by(
            user_id=current_user.id
        ).order_by(Customer.name).all()
        return render_template(
            "quotes/form.html",
            quote=None,
            customers=customers,
            tax_rate=TAX_RATE,
            today=date.today().isoformat(),
            form=request.form,
        ), 400

    excl, incl = _totals(items)
    quote = Quote(
        user_id=current_user.id,
        customer_id=customer.id,
        number=_next_number("DEV", Quote),
        quote_date=quote_date,
        status=status,
        amount_excl_tax=excl,
        amount_incl_tax=incl,
    )
    for it in items:
        quote.items.append(QuoteItem(**it))
    db.session.add(quote)
    db.session.commit()

    log_action(current_user.id, "create_quote", {
        "quote_id": quote.id,
        "number": quote.number,
        "amount_incl_tax": str(incl),
    })
    flash(f"Devis {quote.number} créé avec succès.", "success")
    return redirect(url_for("quotes.show", quote_id=quote.id))


@quotes_bp.route("/<int:quote_id>")
@login_required
@subscription_required
def show(quote_id):
    """Détail d'un devis (lignes + totaux)."""
    quote = _owned_quote_or_404(quote_id)
    linked_invoice = None
    if quote.invoice_id:
        linked_invoice = Invoice.query.filter_by(
            id=quote.invoice_id, user_id=current_user.id
        ).first()
    return render_template(
        "quotes/show.html",
        quote=quote,
        statuses=QUOTE_STATUSES,
        tax_rate=TAX_RATE,
        linked_invoice=linked_invoice,
        fmt=_fmt,
    )


@quotes_bp.route("/<int:quote_id>/edit")
@login_required
@subscription_required
def edit(quote_id):
    """Formulaire d'édition d'un devis."""
    quote = _owned_quote_or_404(quote_id)
    if quote.invoice_id:
        flash("Ce devis est converti en facture : édition impossible.", "warning")
        return redirect(url_for("quotes.show", quote_id=quote.id))
    customers = Customer.query.filter_by(
        user_id=current_user.id
    ).order_by(Customer.name).all()
    return render_template(
        "quotes/form.html",
        quote=quote,
        customers=customers,
        tax_rate=TAX_RATE,
        today=date.today().isoformat(),
    )


@quotes_bp.route("/<int:quote_id>", methods=["POST"])
@login_required
@subscription_required
def update(quote_id):
    """Met à jour un devis et remplace ses lignes."""
    quote = _owned_quote_or_404(quote_id)
    if quote.invoice_id:
        flash("Ce devis est converti en facture : édition impossible.", "warning")
        return redirect(url_for("quotes.show", quote_id=quote.id))

    customer_id = request.form.get("customer_id", type=int)
    quote_date = _parse_date(request.form.get("quote_date"))
    status = request.form.get("status", "draft")

    customer = _owned_customer_or_none(customer_id)
    items, errors = _read_lines()

    if customer is None:
        errors.insert(0, "Sélectionnez un client valide.")
    if quote_date is None:
        errors.append("La date du devis est invalide.")
    if status not in QUOTE_STATUSES:
        status = "draft"

    if errors:
        for msg in errors:
            flash(msg, "danger")
        customers = Customer.query.filter_by(
            user_id=current_user.id
        ).order_by(Customer.name).all()
        return render_template(
            "quotes/form.html",
            quote=quote,
            customers=customers,
            tax_rate=TAX_RATE,
            today=date.today().isoformat(),
            form=request.form,
        ), 400

    excl, incl = _totals(items)
    quote.customer_id = customer.id
    quote.quote_date = quote_date
    quote.status = status
    quote.amount_excl_tax = excl
    quote.amount_incl_tax = incl
    # remplace les lignes (cascade delete-orphan)
    quote.items.clear()
    for it in items:
        quote.items.append(QuoteItem(**it))
    db.session.commit()

    log_action(current_user.id, "update_quote", {
        "quote_id": quote.id,
        "number": quote.number,
        "amount_incl_tax": str(incl),
    })
    flash(f"Devis {quote.number} mis à jour.", "success")
    return redirect(url_for("quotes.show", quote_id=quote.id))


@quotes_bp.route("/<int:quote_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete(quote_id):
    """Supprime un devis du gérant courant."""
    quote = _owned_quote_or_404(quote_id)
    if quote.invoice_id:
        flash("Impossible de supprimer un devis déjà facturé.", "warning")
        return redirect(url_for("quotes.show", quote_id=quote.id))
    number = quote.number
    db.session.delete(quote)
    db.session.commit()

    log_action(current_user.id, "delete_quote", {"quote_id": quote_id, "number": number})
    flash(f"Devis {number} supprimé.", "success")
    return redirect(url_for("quotes.index"))


@quotes_bp.route("/<int:quote_id>/pdf")
@login_required
@subscription_required
def pdf(quote_id):
    """PDF du devis (reportlab)."""
    quote = _owned_quote_or_404(quote_id)
    settings = CompanySettings.query.filter_by(user_id=current_user.id).first()
    buffer = _build_document_pdf(
        title="DEVIS",
        number=quote.number,
        doc_date=quote.quote_date,
        customer=quote.customer,
        items=quote.items,
        amount_excl=quote.amount_excl_tax,
        amount_incl=quote.amount_incl_tax,
        settings=settings,
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{quote.number}.pdf"',
        },
    )


@quotes_bp.route("/<int:quote_id>/to-invoice", methods=["POST"])
@login_required
@subscription_required
def to_invoice(quote_id):
    """Convertit un devis en facture (crée Invoice + InvoiceItems, lie invoice_id)."""
    quote = _owned_quote_or_404(quote_id)
    if quote.invoice_id:
        flash("Ce devis a déjà été converti en facture.", "warning")
        return redirect(url_for("invoices.show", invoice_id=quote.invoice_id))

    invoice = Invoice(
        user_id=current_user.id,
        customer_id=quote.customer_id,
        quote_id=quote.id,
        number=_next_number("FAC", Invoice),
        invoice_date=date.today(),
        status="unpaid",
        amount_excl_tax=quote.amount_excl_tax,
        amount_incl_tax=quote.amount_incl_tax,
    )
    for it in quote.items:
        invoice.items.append(InvoiceItem(
            description=it.description,
            quantity=it.quantity,
            unit_price=it.unit_price,
            amount=it.amount,
        ))
    db.session.add(invoice)
    db.session.flush()  # obtient invoice.id
    quote.invoice_id = invoice.id
    if quote.status != "accepted":
        quote.status = "accepted"
    db.session.commit()

    log_action(current_user.id, "convert_quote_to_invoice", {
        "quote_id": quote.id,
        "quote_number": quote.number,
        "invoice_id": invoice.id,
        "invoice_number": invoice.number,
    })
    flash(
        f"Devis {quote.number} converti en facture {invoice.number}.",
        "success",
    )
    return redirect(url_for("invoices.show", invoice_id=invoice.id))


# ─────────────────────────────────────────────────────────────────────────────
#  FACTURES
# ─────────────────────────────────────────────────────────────────────────────
@invoices_bp.route("/")
@login_required
@subscription_required
def index():
    """Liste des factures du gérant (statut de paiement, client, TTC, date)."""
    status = request.args.get("status", type=str)
    query = Invoice.query.filter_by(user_id=current_user.id)
    if status in INVOICE_STATUSES:
        query = query.filter(Invoice.status == status)

    invoices = query.order_by(
        Invoice.invoice_date.desc(), Invoice.id.desc()
    ).all()

    total_ttc = Decimal("0")
    total_paid = Decimal("0")
    paid_map = {}
    for inv in invoices:
        total_ttc += Decimal(str(inv.amount_incl_tax or 0))
        p = _paid_amount(inv)
        paid_map[inv.id] = p
        total_paid += p

    return render_template(
        "invoices/index.html",
        invoices=invoices,
        statuses=INVOICE_STATUSES,
        selected_status=status if status in INVOICE_STATUSES else None,
        total_ttc=total_ttc,
        total_paid=total_paid,
        total_due=total_ttc - total_paid,
        paid_map=paid_map,
        count=len(invoices),
        fmt=_fmt,
    )


@invoices_bp.route("/<int:invoice_id>")
@login_required
@subscription_required
def show(invoice_id):
    """Détail d'une facture (lignes, totaux, paiements, solde)."""
    invoice = _owned_invoice_or_404(invoice_id)
    paid = _paid_amount(invoice)
    due = Decimal(str(invoice.amount_incl_tax or 0)) - paid
    payments = Payment.query.filter_by(invoice_id=invoice.id).order_by(
        Payment.payment_date.desc(), Payment.id.desc()
    ).all()
    return render_template(
        "invoices/show.html",
        invoice=invoice,
        statuses=INVOICE_STATUSES,
        methods=PAYMENT_METHODS,
        tax_rate=TAX_RATE,
        payments=payments,
        paid=paid,
        due=due if due > 0 else Decimal("0"),
        today=date.today().isoformat(),
        fmt=_fmt,
    )


@invoices_bp.route("/<int:invoice_id>/pdf")
@login_required
@subscription_required
def pdf(invoice_id):
    """PDF de la facture (reportlab)."""
    invoice = _owned_invoice_or_404(invoice_id)
    paid = _paid_amount(invoice)
    due = Decimal(str(invoice.amount_incl_tax or 0)) - paid
    settings = CompanySettings.query.filter_by(user_id=current_user.id).first()
    buffer = _build_document_pdf(
        title="FACTURE",
        number=invoice.number,
        doc_date=invoice.invoice_date,
        customer=invoice.customer,
        items=invoice.items,
        amount_excl=invoice.amount_excl_tax,
        amount_incl=invoice.amount_incl_tax,
        paid=paid,
        due=due if due > 0 else Decimal("0"),
        settings=settings,
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{invoice.number}.pdf"',
        },
    )


@invoices_bp.route("/<int:invoice_id>/payments", methods=["POST"])
@login_required
@subscription_required
def add_payment(invoice_id):
    """Enregistre un paiement et recalcule le statut de la facture."""
    invoice = _owned_invoice_or_404(invoice_id)

    amount = _parse_decimal(request.form.get("amount"))
    pay_date = _parse_date(request.form.get("payment_date"))
    method = request.form.get("method", "cash")

    errors = []
    if amount is None:
        errors.append("Le montant du paiement doit être un nombre supérieur à 0.")
    if pay_date is None:
        errors.append("La date de paiement est invalide.")
    if method not in PAYMENT_METHODS:
        method = "cash"

    # ne pas dépasser le solde restant
    if amount is not None:
        already = _paid_amount(invoice)
        remaining = Decimal(str(invoice.amount_incl_tax or 0)) - already
        if remaining <= 0:
            errors.append("Cette facture est déjà entièrement payée.")
        elif amount > remaining:
            errors.append(
                f"Le montant dépasse le solde restant ({_fmt(remaining)} FCFA)."
            )

    if errors:
        for msg in errors:
            flash(msg, "danger")
        return redirect(url_for("invoices.show", invoice_id=invoice.id))

    payment = Payment(
        invoice_id=invoice.id,
        amount=amount,
        payment_date=pay_date,
        method=method,
    )
    db.session.add(payment)
    db.session.flush()
    paid = _refresh_invoice_status(invoice)
    db.session.commit()

    log_action(current_user.id, "add_payment", {
        "invoice_id": invoice.id,
        "invoice_number": invoice.number,
        "amount": str(amount),
        "method": method,
        "total_paid": str(paid),
        "status": invoice.status,
    })
    flash(f"Paiement de {_fmt(amount)} FCFA enregistré.", "success")
    return redirect(url_for("invoices.show", invoice_id=invoice.id))


@invoices_bp.route("/<int:invoice_id>/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete_payment(invoice_id, payment_id):
    """Supprime un paiement et recalcule le statut."""
    invoice = _owned_invoice_or_404(invoice_id)
    payment = Payment.query.filter_by(
        id=payment_id, invoice_id=invoice.id
    ).first()
    if payment is None:
        abort(404)
    amount = payment.amount
    db.session.delete(payment)
    db.session.flush()
    _refresh_invoice_status(invoice)
    db.session.commit()

    log_action(current_user.id, "delete_payment", {
        "invoice_id": invoice.id,
        "payment_id": payment_id,
        "amount": str(amount),
        "status": invoice.status,
    })
    flash("Paiement supprimé.", "success")
    return redirect(url_for("invoices.show", invoice_id=invoice.id))


@invoices_bp.route("/<int:invoice_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete(invoice_id):
    """Supprime une facture (et détache le devis source le cas échéant)."""
    invoice = _owned_invoice_or_404(invoice_id)
    number = invoice.number

    # détache le devis source pour éviter une FK orpheline
    source_quote = Quote.query.filter_by(
        invoice_id=invoice.id, user_id=current_user.id
    ).first()
    if source_quote:
        source_quote.invoice_id = None

    # supprime les paiements liés puis la facture
    Payment.query.filter_by(invoice_id=invoice.id).delete()
    db.session.delete(invoice)
    db.session.commit()

    log_action(current_user.id, "delete_invoice", {
        "invoice_id": invoice_id, "number": number,
    })
    flash(f"Facture {number} supprimée.", "success")
    return redirect(url_for("invoices.index"))


# ─────────────────────────────────────────────────────────────────────────────
#  Génération PDF (reportlab)
# ─────────────────────────────────────────────────────────────────────────────
def _build_document_pdf(title, number, doc_date, customer, items,
                        amount_excl, amount_incl, paid=None, due=None,
                        settings=None):
    """Construit un PDF (devis ou facture) et renvoie un BytesIO.

    En-tête entreprise (logo + nom + coordonnées des paramètres, sinon SenGestion),
    coordonnées client, tableau des lignes, totaux HT/TVA/TTC, cachet/signature.
    Charte : marine #021A3D, or #F2B10E.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Frame, KeepInFrame,
    )
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
    import os
    from flask import current_app
    from reportlab.platypus import Image as RLImage

    # ── Palette (charte stricte 3 couleurs + neutres dérivés) ──
    MARINE = colors.HexColor("#021A3D")
    OR = colors.HexColor("#F2B10E")
    JAUNE_PALE = colors.HexColor("#E8E7A2")
    # Neutres à légère teinte marine (choisis, pas des gris purs).
    INK_SOFT = colors.HexColor("#4A5568")     # texte secondaire
    ROW_ALT = colors.HexColor("#F5F7FA")      # zébrure de tableau
    HAIRLINE = colors.HexColor("#E3E7ED")     # filets fins
    CARD_BG = colors.HexColor("#F8F9FB")      # fond des cartes info

    PAGE_W, PAGE_H = A4
    ML = MR = 16 * mm
    CONTENT_W = PAGE_W - ML - MR
    HEADER_H = 56 * mm                        # hauteur de la zone d'en-tête (jusqu'au liseré or)

    def _abs(rel):
        if not rel:
            return None
        path = os.path.join(current_app.static_folder, rel)
        return path if os.path.exists(path) else None

    # ── Données émetteur ──
    brand_name = (settings.company_name if settings and settings.company_name else "SenGestion")
    issuer_lines = []
    if settings:
        if settings.address:
            issuer_lines.append(str(settings.address).replace("\n", ", "))
        for b in (settings.phone, settings.email, settings.website):
            if b:
                issuer_lines.append(b)
        ids = []
        if settings.ninea:
            ids.append(f"NINEA {settings.ninea}")
        if settings.rccm:
            ids.append(f"RCCM {settings.rccm}")
        if ids:
            issuer_lines.append(" · ".join(ids))
    logo_path = _abs(settings.logo) if settings else None
    d = doc_date.strftime("%d/%m/%Y") if doc_date else ""

    # ── Styles typographiques (échelle marquée) ──
    styles = getSampleStyleSheet()
    st_brand = ParagraphStyle("brand", fontName="Times-Bold", fontSize=22,
                              textColor=colors.white, leading=24)
    st_issuer = ParagraphStyle("issuer", fontName="Helvetica", fontSize=8.2,
                               textColor=colors.HexColor("#C7D0DE"), leading=12)
    st_doctype = ParagraphStyle("doctype", fontName="Times-Bold", fontSize=30,
                                textColor=OR, alignment=TA_RIGHT, leading=32)
    st_docnum = ParagraphStyle("docnum", fontName="Helvetica-Bold", fontSize=11,
                               textColor=colors.white, alignment=TA_RIGHT, leading=16)
    st_label = ParagraphStyle("label", fontName="Helvetica-Bold", fontSize=9,
                              textColor=OR, leading=13)   # eyebrow (micro-label)
    st_body = ParagraphStyle("body", fontName="Helvetica", fontSize=12,
                             textColor=MARINE, leading=17)     # texte courant = 12 pt (charte)
    st_body_b = ParagraphStyle("bodyb", parent=st_body, fontName="Helvetica-Bold")
    st_muted = ParagraphStyle("muted", fontName="Helvetica", fontSize=10,
                              textColor=INK_SOFT, leading=15)
    st_cell = ParagraphStyle("cell", fontName="Helvetica", fontSize=12,
                             textColor=MARINE, leading=16)     # cellules tableau = 12 pt

    buffer = BytesIO()

    # ── En-tête blanc épuré + pied de page dessinés sur chaque page ──
    def _decorate(canvas, doc_):
        canvas.saveState()
        top = PAGE_H - 16 * mm

        # Marque : logo si dispo, sinon nom en marine.
        # coord_y = position de départ des coordonnées, sous la marque (jamais de chevauchement).
        LOGO_MAX_H = 15 * mm
        if logo_path:
            try:
                img = RLImage(logo_path, width=44 * mm, height=LOGO_MAX_H, kind="proportional")
                iw, ih = img.wrap(0, 0)
                img.drawOn(canvas, ML, PAGE_H - 12 * mm - ih)
                coord_y = PAGE_H - 12 * mm - ih - 7 * mm   # marge nette sous le logo
            except Exception:
                canvas.setFillColor(MARINE)
                canvas.setFont("Times-Bold", 24)
                canvas.drawString(ML, top - 8, brand_name)
                coord_y = PAGE_H - 24 * mm
        else:
            canvas.setFillColor(MARINE)
            canvas.setFont("Times-Bold", 24)
            canvas.drawString(ML, top - 8, brand_name)
            coord_y = PAGE_H - 24 * mm

        # Coordonnées émetteur (12 pt — charte typographique)
        canvas.setFillColor(INK_SOFT)
        canvas.setFont("Helvetica", 12)
        y = coord_y
        for line in issuer_lines[:3]:
            canvas.drawString(ML, y, line[:95])
            y -= 5.6 * mm

        # Titre document (DEVIS / FACTURE) en marine + numéro à droite
        canvas.setFillColor(MARINE)
        canvas.setFont("Times-Bold", 32)
        canvas.drawRightString(PAGE_W - MR, top - 6, title)
        canvas.setFillColor(MARINE)
        canvas.setFont("Helvetica-Bold", 10.5)
        canvas.drawRightString(PAGE_W - MR, top - 13 * mm, f"N° {number}")
        canvas.setFillColor(INK_SOFT)
        canvas.setFont("Helvetica", 8.5)
        canvas.drawRightString(PAGE_W - MR, top - 17 * mm, f"Date : {d}")

        # Liseré or épais sous l'en-tête (le seul aplat de couleur)
        canvas.setFillColor(OR)
        canvas.rect(ML, PAGE_H - HEADER_H, CONTENT_W, 2.5, fill=1, stroke=0)

        # ── Pied de page ──
        canvas.setFillColor(HAIRLINE)
        canvas.rect(ML, 15 * mm, CONTENT_W, 0.6, fill=1, stroke=0)
        canvas.setFillColor(INK_SOFT)
        canvas.setFont("Helvetica", 7.6)
        canvas.drawString(ML, 11 * mm,
                          f"{brand_name} — Montants exprimés en francs CFA (FCFA).")
        canvas.drawRightString(PAGE_W - MR, 11 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=ML, rightMargin=MR,
        topMargin=HEADER_H + 8 * mm,          # contenu commence sous le bandeau
        bottomMargin=20 * mm,
        title=f"{title} {number}",
    )

    story = []

    # ── Carte CLIENT à gauche | récapitulatif à droite ──
    # (L'émetteur figure déjà dans l'en-tête : pas de carte Émetteur pour éviter la répétition.)
    cust_name = customer.name if customer else "—"
    cust_html_lines = [f"<b>{cust_name}</b>"]
    if customer:
        if customer.company:
            cust_html_lines.append(customer.company)
        if customer.email:
            cust_html_lines.append(customer.email)
        if customer.phone:
            cust_html_lines.append(customer.phone)
        if customer.address:
            cust_html_lines.append(str(customer.address).replace("\n", ", "))
    cust_html = "<br/>".join(cust_html_lines)

    card_label = "FACTURÉ À" if title.strip().upper().startswith("FAC") else "DESTINATAIRE"
    # Carte destinataire (moitié droite : convention — le client s'aligne à droite,
    # face à l'émetteur de l'en-tête qui est à gauche).
    client_card = Table(
        [[Paragraph(card_label, st_label)],
         [Paragraph(cust_html, st_body)]],
        colWidths=[CONTENT_W / 2],
    )
    client_card.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (0, 0), 10),
        ("BOTTOMPADDING", (0, 0), (0, 0), 3),
        ("TOPPADDING", (0, 1), (0, 1), 2),
        ("BOTTOMPADDING", (0, 1), (0, 1), 12),
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("LINEBELOW", (0, 0), (-1, 0), 2, OR),
    ]))

    # Placée à droite ; la moitié gauche reste vide (aération).
    holder = Table([["", client_card]], colWidths=[CONTENT_W / 2, CONTENT_W / 2])
    holder.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 4 * mm),
    ]))
    story.append(holder)
    story.append(Spacer(1, 9 * mm))

    # ── Tableau des lignes ──
    col_desc = CONTENT_W - (16 * mm + 30 * mm + 34 * mm)
    data = [[
        Paragraph("DÉSIGNATION", ParagraphStyle("th", fontName="Helvetica-Bold",
                  fontSize=8.5, textColor=colors.white, leading=11)),
        Paragraph("QTÉ", ParagraphStyle("thr", fontName="Helvetica-Bold", fontSize=8.5,
                  textColor=colors.white, alignment=TA_CENTER, leading=11)),
        Paragraph("P.U.", ParagraphStyle("thr2", fontName="Helvetica-Bold", fontSize=8.5,
                  textColor=colors.white, alignment=TA_RIGHT, leading=11)),
        Paragraph("MONTANT", ParagraphStyle("thr3", fontName="Helvetica-Bold", fontSize=8.5,
                  textColor=colors.white, alignment=TA_RIGHT, leading=11)),
    ]]
    st_num = ParagraphStyle("num", fontName="Helvetica", fontSize=12,
                            textColor=MARINE, alignment=TA_RIGHT, leading=16)
    st_num_c = ParagraphStyle("numc", parent=st_num, alignment=TA_CENTER)
    for it in items:
        data.append([
            Paragraph(str(it.description), st_cell),
            Paragraph(_fmt(it.quantity), st_num_c),
            Paragraph(_fmt(it.unit_price), st_num),
            Paragraph(_fmt(it.amount), st_num),
        ])
    table = Table(data, colWidths=[col_desc, 16 * mm, 30 * mm, 34 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        # En-tête marine
        ("BACKGROUND", (0, 0), (-1, 0), MARINE),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        # Corps
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ROW_ALT]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, HAIRLINE),
        ("LINEBELOW", (0, -1), (-1, -1), 1.2, MARINE),
    ]))
    story.append(table)
    story.append(Spacer(1, 7 * mm))

    # ── Bloc totaux (encadré marine, TTC en or) ──
    tva = (Decimal(str(amount_incl or 0)) - Decimal(str(amount_excl or 0)))
    st_tot_lbl = ParagraphStyle("tl", fontName="Helvetica", fontSize=12,
                                textColor=INK_SOFT, leading=16)
    st_tot_val = ParagraphStyle("tv", fontName="Helvetica-Bold", fontSize=12,
                                textColor=MARINE, alignment=TA_RIGHT, leading=16)
    st_ttc_lbl = ParagraphStyle("ttcl", fontName="Helvetica-Bold", fontSize=13,
                                textColor=MARINE, leading=17)
    st_ttc_val = ParagraphStyle("ttcv", fontName="Helvetica-Bold", fontSize=13,
                                textColor=MARINE, alignment=TA_RIGHT, leading=17)

    rows = [
        [Paragraph("Total HT", st_tot_lbl), Paragraph(f"{_fmt(amount_excl)} FCFA", st_tot_val)],
        [Paragraph(f"TVA ({int(TAX_RATE * 100)} %)", st_tot_lbl),
         Paragraph(f"{_fmt(tva)} FCFA", st_tot_val)],
    ]
    ttc_row_idx = len(rows)
    rows.append([Paragraph("TOTAL TTC", st_ttc_lbl),
                 Paragraph(f"{_fmt(amount_incl)} FCFA", st_ttc_val)])
    extra_start = None
    if paid is not None:
        extra_start = len(rows)
        rows.append([Paragraph("Déjà réglé", st_tot_lbl),
                     Paragraph(f"{_fmt(paid)} FCFA", st_tot_val)])
        rows.append([Paragraph("Reste à payer", st_ttc_lbl),
                     Paragraph(f"{_fmt(due)} FCFA", st_ttc_val)])

    totals = Table(rows, colWidths=[38 * mm, 46 * mm], hAlign="RIGHT")
    ts = [
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, ttc_row_idx - 1), 0.5, HAIRLINE),
        # Ligne TTC en jaune pâle, encadrée
        ("BACKGROUND", (0, ttc_row_idx), (-1, ttc_row_idx), JAUNE_PALE),
        ("TOPPADDING", (0, ttc_row_idx), (-1, ttc_row_idx), 9),
        ("BOTTOMPADDING", (0, ttc_row_idx), (-1, ttc_row_idx), 9),
        ("LINEABOVE", (0, ttc_row_idx), (-1, ttc_row_idx), 1.5, OR),
    ]
    if extra_start is not None:
        ts.append(("BACKGROUND", (0, extra_start + 1), (-1, extra_start + 1), JAUNE_PALE))
        ts.append(("LINEABOVE", (0, extra_start + 1), (-1, extra_start + 1), 1.5, OR))
        ts.append(("TOPPADDING", (0, extra_start + 1), (-1, extra_start + 1), 9))
        ts.append(("BOTTOMPADDING", (0, extra_start + 1), (-1, extra_start + 1), 9))
    totals.setStyle(TableStyle(ts))
    story.append(totals)
    story.append(Spacer(1, 10 * mm))

    story.append(Spacer(1, 4 * mm))

    # ── Encadré "Conditions & validité" (pleine largeur, équilibre la page) ──
    is_devis = title.strip().upper().startswith("DEV")
    cond_lines = []
    if is_devis:
        cond_lines.append("Devis valable 30 jours à compter de la date d'émission.")
        cond_lines.append("Le règlement vaut acceptation des conditions ci-dessus.")
    else:
        cond_lines.append("Règlement à réception de facture.")
    if settings and settings.footer_note:
        # les conditions personnalisées passent en premier
        cond_lines = [str(settings.footer_note).replace("\n", "<br/>")] + (
            cond_lines if not is_devis else cond_lines[:1])

    cond_html = ("<font size=9 color='#F2B10E'><b>CONDITIONS &amp; VALIDITÉ</b></font><br/>"
                 + "<br/>".join(cond_lines))
    cond_box = Table([[Paragraph(cond_html, st_muted)]], colWidths=[CONTENT_W])
    cond_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("LINEABOVE", (0, 0), (-1, 0), 2, OR),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(cond_box)
    story.append(Spacer(1, 8 * mm))

    # ── Cachet signé (aligné à droite) ──
    stamp_path = _abs(settings.stamp) if settings else None
    if stamp_path:
        try:
            stamp_cell = Table(
                [[Paragraph("<font size=9 color='#F2B10E'><b>CACHET &amp; SIGNATURE</b></font>", st_muted)],
                 [RLImage(stamp_path, width=45 * mm, height=30 * mm, kind="proportional")]],
                colWidths=[50 * mm], hAlign="RIGHT",
            )
            stamp_cell.setStyle(TableStyle([
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (0, 0), 0),
                ("BOTTOMPADDING", (0, 0), (0, 0), 4),
            ]))
            story.append(stamp_cell)
        except Exception:
            pass

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    buffer.seek(0)
    return buffer
