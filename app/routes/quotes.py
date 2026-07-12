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


_UNITS = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit",
          "neuf", "dix", "onze", "douze", "treize", "quatorze", "quinze", "seize",
          "dix-sept", "dix-huit", "dix-neuf"]
_TENS = {20: "vingt", 30: "trente", 40: "quarante", 50: "cinquante",
         60: "soixante", 80: "quatre-vingt"}


def _n2w(n):
    """Nombre entier -> lettres (français). Suffisant pour les montants FCFA."""
    n = int(n)
    if n < 20:
        return _UNITS[n]
    if n < 100:
        if n < 70:
            t = (n // 10) * 10
            u = n % 10
            if u == 0:
                return _TENS[t]
            if u == 1 and t in (20, 30, 40, 50, 60):
                return f"{_TENS[t]}-et-un"
            return f"{_TENS[t]}-{_UNITS[u]}"
        if n < 80:
            return "soixante-" + (_n2w(n - 60) if n != 71 else "et-onze")
        # 80..99
        u = n - 80
        if u == 0:
            return "quatre-vingts"
        return "quatre-vingt-" + _n2w(u)
    if n < 1000:
        h = n // 100
        r = n % 100
        head = ("cent" if h == 1 else f"{_UNITS[h]}-cent")
        if r == 0:
            return head + ("s" if h > 1 else "")
        return f"{head}-{_n2w(r)}"
    if n < 1_000_000:
        th = n // 1000
        r = n % 1000
        head = "mille" if th == 1 else f"{_n2w(th)}-mille"
        return head if r == 0 else f"{head}-{_n2w(r)}"
    if n < 1_000_000_000:
        m = n // 1_000_000
        r = n % 1_000_000
        head = f"{_n2w(m)}-million" + ("s" if m > 1 else "")
        return head if r == 0 else f"{head}-{_n2w(r)}"
    mrd = n // 1_000_000_000
    r = n % 1_000_000_000
    head = f"{_n2w(mrd)}-milliard" + ("s" if mrd > 1 else "")
    return head if r == 0 else f"{head}-{_n2w(r)}"


def _amount_words(value):
    """Montant FCFA en toutes lettres, capitalisé."""
    try:
        n = int(Decimal(str(value or 0)))
    except (InvalidOperation, ValueError):
        n = 0
    words = _n2w(n)
    return (words[0].upper() + words[1:]) + " francs CFA"


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


@quotes_bp.route("/voice")
@login_required
@subscription_required
def voice():
    """Page de dictée d'un devis par commande vocale (Web Speech API)."""
    return render_template("quotes/voice.html")


@quotes_bp.route("/voice/transcribe", methods=["POST"])
@login_required
@subscription_required
def voice_transcribe():
    """Reçoit un audio (MediaRecorder), le transcrit (Whisper), renvoie le texte JSON.

    Compatible tous navigateurs (Safari inclus) : l'enregistrement audio est
    universel, contrairement à la Web Speech API réservée à Chrome.
    """
    from flask import jsonify
    from app.services.transcription_service import transcribe_audio

    audio = request.files.get("audio")
    if audio is None or not audio.filename:
        return jsonify({"ok": False, "error": "Aucun audio reçu."}), 400

    data = audio.read()
    # Garde-fou taille (MAX_CONTENT_LENGTH global gère déjà, mais message clair).
    result = transcribe_audio(data, filename=audio.filename)
    log_action(current_user.id, "voice_transcribe",
               {"ok": result.get("ok"), "bytes": len(data)})
    return jsonify(result), (200 if result.get("ok") else 422)


@quotes_bp.route("/voice", methods=["POST"])
@login_required
@subscription_required
def voice_parse():
    """Reçoit le texte dicté, l'interprète via Claude, pré-remplit le formulaire."""
    from app.services.voice_quote_service import parse_voice_quote
    from werkzeug.datastructures import MultiDict

    transcript = (request.form.get("transcript") or "").strip()
    customers = Customer.query.filter_by(user_id=current_user.id).order_by(Customer.name).all()
    result = parse_voice_quote(transcript, [c.name for c in customers])

    log_action(current_user.id, "voice_quote",
               {"ok": result.get("ok"), "len": len(transcript)})

    if not result.get("ok"):
        flash(result.get("error", "Commande vocale non comprise."), "danger")
        return redirect(url_for("quotes.voice"))

    # Match du client par nom (insensible à la casse) parmi les clients du gérant.
    matched_id = None
    wanted = (result.get("customer_name") or "").strip().lower()
    for c in customers:
        if c.name.strip().lower() == wanted:
            matched_id = c.id
            break

    # Construire un MultiDict pour pré-remplir le formulaire (comme un POST rejoué).
    form = MultiDict()
    if matched_id:
        form["customer_id"] = str(matched_id)
    form["quote_date"] = date.today().isoformat()
    for it in result["items"]:
        form.add("designation[]", it["description"])
        form.add("quantity[]", str(it["quantity"]))
        form.add("unit_price[]", str(it["unit_price"]))

    note = "Devis pré-rempli par commande vocale : vérifiez le client, les articles et les prix."
    if not matched_id and result.get("customer_name"):
        note += (f" Client « {result['customer_name']} » non trouvé dans votre carnet — "
                 "sélectionnez-le ou créez-le d'abord.")
    flash(note, "success")

    return render_template(
        "quotes/form.html",
        quote=None,
        customers=customers,
        tax_rate=TAX_RATE,
        today=date.today().isoformat(),
        form=form,
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
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
    )
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
    import os
    from flask import current_app
    from reportlab.platypus import Image as RLImage
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # ── Police de titre : Palatino (charte). Fallback Times si indisponible. ──
    # On tente d'abord la police embarquée dans le projet, puis celle du système.
    def _register_palatino():
        candidates = [
            os.path.join(current_app.static_folder, "fonts", "Palatino.ttc"),
            "/System/Library/Fonts/Palatino.ttc",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    if "Palatino" not in pdfmetrics.getRegisteredFontNames():
                        pdfmetrics.registerFont(TTFont("Palatino", path, subfontIndex=0))
                        pdfmetrics.registerFont(TTFont("Palatino-Bold", path, subfontIndex=2))
                    return "Palatino", "Palatino-Bold"
                except Exception:
                    continue
        # Fallback : Times (serif intégré, proche de Palatino — cf. CSS fallback)
        return "Times-Roman", "Times-Bold"

    FONT_TITLE, FONT_TITLE_BOLD = _register_palatino()

    # ── Police de texte : Arial (charte UI). Fallback Helvetica si indisponible. ──
    def _register_arial():
        fdir = os.path.join(current_app.static_folder, "fonts")
        files = {
            "Arial": [os.path.join(fdir, "Arial.ttf"),
                      "/System/Library/Fonts/Supplemental/Arial.ttf"],
            "Arial-Bold": [os.path.join(fdir, "Arial-Bold.ttf"),
                           "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
            "Arial-Italic": [os.path.join(fdir, "Arial-Italic.ttf"),
                             "/System/Library/Fonts/Supplemental/Arial Italic.ttf"],
        }
        ok = True
        for name, paths in files.items():
            if name in pdfmetrics.getRegisteredFontNames():
                continue
            registered = False
            for p in paths:
                if os.path.exists(p):
                    try:
                        pdfmetrics.registerFont(TTFont(name, p))
                        registered = True
                        break
                    except Exception:
                        continue
            if not registered:
                ok = False
        if ok:
            return "Arial", "Arial-Bold", "Arial-Italic"
        # Fallback : Helvetica (clone d'Arial, fallback officiel de la charte)
        return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"

    FONT_BODY, FONT_BODY_BOLD, FONT_BODY_ITALIC = _register_arial()

    # ── Palette — CHARTE STRICTE 3 couleurs (marine / or / jaune pâle) ──
    # Les neutres sont des teintes DÉRIVÉES du marine (pas des gris purs), pour
    # rester dans l'esprit charte comme le reste de l'app (rgba(2,26,61,...)).
    MARINE = colors.HexColor("#021A3D")
    OR = colors.HexColor("#F2B10E")
    JAUNE_PALE = colors.HexColor("#E8E7A2")
    INK_SOFT = MARINE                          # texte secondaire = MARINE (charte, sans ambiguïté)
    INK_FAINT = MARINE                         # labels = MARINE (charte)
    # Neutres = blanc + marine dilué (surfaces neutres, pas des couleurs de marque)
    ROW_ALT = colors.Color(2/255, 26/255, 61/255, 0.04)    # zébrure : marine 4 %
    HAIRLINE = colors.Color(2/255, 26/255, 61/255, 0.14)   # filets marine 14 %
    CREAM = colors.white                       # fond en-tête = BLANC (surface neutre)
    OBJ_BG = JAUNE_PALE                        # carte Objet = jaune pâle (charte)
    CARD_BG = colors.Color(2/255, 26/255, 61/255, 0.035)   # fond cartes : marine 3.5 %

    PAGE_W, PAGE_H = A4
    ML = MR = 15 * mm
    CONTENT_W = PAGE_W - ML - MR
    HEADER_H = 40 * mm

    def _abs(rel):
        if not rel:
            return None
        path = os.path.join(current_app.static_folder, rel)
        return path if os.path.exists(path) else None

    # ── Données émetteur ──
    brand_name = (settings.company_name if settings and settings.company_name else "SenGestion")
    addr = str(settings.address).replace("\n", ", ") if settings and settings.address else ""
    phone = settings.phone if settings else ""
    email = settings.email if settings else ""
    website = settings.website if settings else ""
    # Ligne d'identifiants légaux
    legal_bits = []
    if settings:
        if settings.rc:
            legal_bits.append(f"RC : {settings.rc}")
        if settings.rccm:
            legal_bits.append(f"RCCM : {settings.rccm}")
        if settings.ninea:
            legal_bits.append(f"NINEA : {settings.ninea}")
        lf = " ".join(x for x in (settings.legal_form, ("- Cap. " + settings.capital)
                      if settings.capital else "") if x).strip()
        if lf:
            legal_bits.append(lf)
    legal_line = "  |  ".join(legal_bits)

    logo_path = _abs(settings.logo) if settings else None
    stamp_path = _abs(settings.stamp) if settings else None
    d_emis = doc_date.strftime("%d/%m/%Y") if doc_date else ""
    # Validité : +30 jours pour un devis
    d_valid = ""
    if doc_date:
        from datetime import timedelta
        d_valid = (doc_date + timedelta(days=30)).strftime("%d/%m/%Y")
    is_devis = title.strip().upper().startswith("DEV")

    # ── Styles (12 pt texte courant) ──
    st_eyebrow = ParagraphStyle("eb", fontName=FONT_BODY_BOLD, fontSize=9,
                                textColor=INK_SOFT, leading=12)  # letter-spaced simulé
    st_label = ParagraphStyle("lbl", fontName=FONT_BODY_BOLD, fontSize=8.5,
                              textColor=INK_SOFT, leading=12)
    st_body = ParagraphStyle("body", fontName=FONT_BODY, fontSize=12,
                             textColor=MARINE, leading=16)
    st_body_b = ParagraphStyle("bodyb", parent=st_body, fontName=FONT_BODY_BOLD)
    st_big = ParagraphStyle("big", fontName=FONT_BODY_BOLD, fontSize=15,
                            textColor=MARINE, leading=18)
    st_muted = ParagraphStyle("muted", fontName=FONT_BODY, fontSize=10.5,
                              textColor=INK_SOFT, leading=15)
    st_cell = ParagraphStyle("cell", fontName=FONT_BODY, fontSize=11,
                             textColor=MARINE, leading=15)
    st_num = ParagraphStyle("num", fontName=FONT_BODY, fontSize=11,
                            textColor=INK_SOFT, alignment=TA_RIGHT, leading=15)
    st_num_c = ParagraphStyle("numc", parent=st_num, alignment=TA_CENTER)
    st_num_b = ParagraphStyle("numb", fontName=FONT_BODY_BOLD, fontSize=11,
                              textColor=MARINE, alignment=TA_RIGHT, leading=15)

    buffer = BytesIO()

    # ── En-tête (fond crème) + pied de page ──
    def _decorate(canvas, doc_):
        canvas.saveState()
        # Fond crème de l'en-tête
        canvas.setFillColor(CREAM)
        canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
        # Double filet marine sous l'en-tête
        canvas.setFillColor(MARINE)
        canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, 2.5, fill=1, stroke=0)

        # Logo
        y_logo_bottom = PAGE_H - 24 * mm
        x_text = ML
        if logo_path:
            try:
                img = RLImage(logo_path, width=30 * mm, height=14 * mm, kind="proportional")
                iw, ih = img.wrap(0, 0)
                img.drawOn(canvas, ML, PAGE_H - 20 * mm)
            except Exception:
                pass
        # Nom entreprise (titre de marque — Palatino, charte)
        canvas.setFillColor(MARINE)
        canvas.setFont(FONT_TITLE_BOLD, 20)          # Palatino 20 pt
        canvas.drawString(ML + 34 * mm, PAGE_H - 15 * mm, brand_name)
        # Coordonnées
        canvas.setFillColor(INK_SOFT)
        canvas.setFont(FONT_BODY, 10)
        y = PAGE_H - 24 * mm
        if addr:
            canvas.drawString(ML, y, addr[:90]); y -= 4.6 * mm
        tel_bits = " / ".join(x for x in (phone,) if x)
        if tel_bits:
            canvas.drawString(ML, y, "Tél : " + tel_bits); y -= 4.6 * mm
        if email:
            canvas.drawString(ML, y, email); y -= 4.6 * mm
        # Ligne légale (plus petite, tout en bas de l'en-tête)
        if legal_line:
            canvas.setFont(FONT_BODY, 8)
            canvas.setFillColor(INK_FAINT)
            canvas.drawString(ML, PAGE_H - HEADER_H + 3 * mm, legal_line[:130])

        # Bloc numéro à droite
        canvas.setFillColor(INK_FAINT)
        canvas.setFont(FONT_BODY_BOLD, 9)
        canvas.drawRightString(PAGE_W - MR, PAGE_H - 15 * mm,
                               ("NUMÉRO DE DEVIS" if is_devis else "NUMÉRO DE FACTURE"))
        canvas.setFillColor(MARINE)
        canvas.setFont(FONT_TITLE_BOLD, 24)          # Palatino 24 pt (charte titres)
        canvas.drawRightString(PAGE_W - MR, PAGE_H - 23 * mm, number)
        # petit point or décoratif
        canvas.setFillColor(OR)
        canvas.circle(PAGE_W - MR - 2, PAGE_H - 26.5 * mm, 1.6, fill=1, stroke=0)
        # Dates
        canvas.setFillColor(INK_SOFT)
        canvas.setFont(FONT_BODY, 10)
        canvas.drawRightString(PAGE_W - MR, PAGE_H - 31.5 * mm, f"Émis le {d_emis}")
        if is_devis and d_valid:
            canvas.drawRightString(PAGE_W - MR, PAGE_H - 36 * mm, f"Valide jusqu'au {d_valid}")

        # ── Pied de page ──
        canvas.setFillColor(HAIRLINE)
        canvas.rect(ML, 15 * mm, CONTENT_W, 0.6, fill=1, stroke=0)
        canvas.setFillColor(INK_SOFT)
        canvas.setFont(FONT_BODY, 7.6)
        canvas.drawString(ML, 11 * mm,
                          f"{brand_name} - Montants exprimés en francs CFA (FCFA).")
        canvas.drawRightString(PAGE_W - MR, 11 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=ML, rightMargin=MR,
        topMargin=HEADER_H + 3 * mm, bottomMargin=11 * mm,
        title=f"{title} {number}",
    )
    story = []

    # ── Eyebrow DESTINATAIRE ──
    def _eyebrow(text):
        # Interlettrage typographique propre (charSpace) : espace les lettres SANS
        # confondre les espaces entre mots. Un espace normal sépare bien les mots.
        p = Paragraph(f"<font color='#021A3D'><b>{text}</b></font>",
                      ParagraphStyle("ey", fontName=FONT_BODY_BOLD, fontSize=9,
                                     textColor=INK_SOFT, leading=12, charSpace=3))
        bar = Table([[""]], colWidths=[26 * mm], rowHeights=[2])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), OR)]))
        wrap = Table([[p], [bar]], colWidths=[CONTENT_W])
        wrap.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (0, 0), 0), ("BOTTOMPADDING", (0, 0), (0, 0), 4),
            ("TOPPADDING", (0, 1), (0, 1), 0), ("BOTTOMPADDING", (0, 1), (0, 1), 0),
        ]))
        return wrap

    story.append(_eyebrow("DESTINATAIRE"))
    story.append(Spacer(1, 2 * mm))

    # ── Carte CLIENT (bordée) | carte OBJET (fond bleu) ──
    cust_name = customer.name if customer else "-"
    cust_extra = []
    if customer:
        if customer.company:
            cust_extra.append(customer.company)
        if customer.email:
            cust_extra.append(customer.email)
        if customer.phone:
            cust_extra.append(customer.phone)
    client_body = f"<font size=8.5 color='#021A3D'><b>CLIENT</b></font><br/><b>{cust_name}</b>"
    if cust_extra:
        client_body += "<br/>" + "<br/>".join(cust_extra)
    client_card = Table([[Paragraph(client_body, st_body)]], colWidths=[CONTENT_W * 0.60 - 3 * mm])
    client_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 1.2, MARINE),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))

    obj_text = (f"<font size=8.5 color='#021A3D'><b>OBJET</b></font><br/>"
                f"{('Devis de prestation' if is_devis else 'Facture')}")
    if settings and getattr(settings, "footer_note", None):
        pass
    obj_card = Table([[Paragraph(obj_text, st_body)]], colWidths=[CONTENT_W * 0.40 - 3 * mm])
    obj_card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), OBJ_BG),
        ("ROUNDEDCORNERS", [6, 6, 6, 6]),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
    ]))

    dest = Table([[client_card, obj_card]],
                 colWidths=[CONTENT_W * 0.60, CONTENT_W * 0.40])
    dest.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 3 * mm),
        ("LEFTPADDING", (1, 0), (1, 0), 3 * mm), ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    story.append(dest)
    story.append(Spacer(1, 4.5 * mm))

    # ── Eyebrow + Tableau des prestations ──
    story.append(_eyebrow("DÉTAIL DES PRESTATIONS"))
    story.append(Spacer(1, 2.5 * mm))

    col_desc = CONTENT_W - (24 * mm + 34 * mm + 40 * mm)
    thst = ParagraphStyle("thh", fontName=FONT_BODY_BOLD, fontSize=8.5,
                          textColor=colors.white, leading=11)
    thr = ParagraphStyle("thr", parent=thst, alignment=TA_RIGHT)
    thc = ParagraphStyle("thc", parent=thst, alignment=TA_CENTER)
    data = [[Paragraph("DESCRIPTION", thst), Paragraph("QTÉ", thc),
             Paragraph("PRIX UNITAIRE", thr), Paragraph("TOTAL HT", thr)]]
    for it in items:
        data.append([
            Paragraph(str(it.description), st_cell),
            Paragraph(_fmt(it.quantity), st_num_c),
            Paragraph(f"{_fmt(it.unit_price)} FCFA", st_num),
            Paragraph(f"{_fmt(it.amount)} FCFA", st_num_b),
        ])
    table = Table(data, colWidths=[col_desc, 24 * mm, 34 * mm, 40 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), MARINE),
        ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 6), ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ROW_ALT]),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, HAIRLINE),
        ("LINEBELOW", (0, -1), (-1, -1), 1.5, MARINE),
    ]))
    story.append(table)
    story.append(Spacer(1, 2.5 * mm))

    # ── Bloc totaux encadré (à droite) ──
    tva = (Decimal(str(amount_incl or 0)) - Decimal(str(amount_excl or 0)))
    tva_pct = 0 if (amount_excl in (None, 0) or tva == 0) else int(TAX_RATE * 100)
    st_tl = ParagraphStyle("tl", fontName=FONT_BODY, fontSize=12, textColor=INK_SOFT, leading=16)
    st_tv = ParagraphStyle("tv", fontName=FONT_BODY, fontSize=12, textColor=MARINE,
                           alignment=TA_RIGHT, leading=16)
    st_ttcl = ParagraphStyle("ttl", fontName=FONT_BODY_BOLD, fontSize=12,
                             textColor=colors.white, leading=16)
    st_ttcv = ParagraphStyle("ttv", fontName=FONT_BODY_BOLD, fontSize=12,
                             textColor=colors.white, alignment=TA_RIGHT, leading=16)
    st_words = ParagraphStyle("wd", fontName=FONT_BODY, fontSize=12,
                              textColor=INK_SOFT, leading=16)

    inner_rows = [
        [Paragraph("Sous-total HT", st_tl), Paragraph(f"{_fmt(amount_excl)} FCFA", st_tv)],
        [Paragraph(f"TVA ({tva_pct}.00%)", st_tl), Paragraph(f"{_fmt(tva)} FCFA", st_tv)],
        [Paragraph("TOTAL TTC", st_ttcl), Paragraph(f"{_fmt(amount_incl)} FCFA", st_ttcv)],
    ]
    ttc_i = 2
    # 'Déjà réglé / Reste à payer' : uniquement si au moins un paiement enregistré.
    show_payment_lines = paid is not None and Decimal(str(paid or 0)) > 0
    if show_payment_lines:
        inner_rows.append([Paragraph("Déjà réglé", st_tl), Paragraph(f"{_fmt(paid)} FCFA", st_tv)])
        inner_rows.append([Paragraph("Reste à payer", st_ttcl),
                           Paragraph(f"{_fmt(due)} FCFA", st_ttcv)])
    # Largeur du bloc = moitié droite du contenu (colle au bord droit).
    box_w = CONTENT_W / 2 + 6 * mm
    inner = Table(inner_rows, colWidths=[box_w * 0.52, box_w * 0.48])
    inner_style = [
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, HAIRLINE),
        ("BACKGROUND", (0, ttc_i), (-1, ttc_i), MARINE),
        ("TOPPADDING", (0, ttc_i), (-1, ttc_i), 11), ("BOTTOMPADDING", (0, ttc_i), (-1, ttc_i), 11),
    ]
    if show_payment_lines:
        # ligne 4 = "Reste à payer" (fond marine, comme le TTC)
        inner_style.append(("BACKGROUND", (0, 4), (-1, 4), MARINE))
        inner_style.append(("TOPPADDING", (0, 4), (-1, 4), 11))
        inner_style.append(("BOTTOMPADDING", (0, 4), (-1, 4), 11))
    inner.setStyle(TableStyle(inner_style))

    words_p = Paragraph(_amount_words(amount_incl), st_words)
    # Box collée à droite (hAlign RIGHT), largeur = celle des totaux internes.
    box = Table([[inner], [words_p]], colWidths=[box_w], hAlign="RIGHT")
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, HAIRLINE),
        ("ROUNDEDCORNERS", [8, 8, 8, 8]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (0, 0), 6), ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (0, 1), 6), ("BOTTOMPADDING", (0, 1), (0, 1), 8),
        ("LEFTPADDING", (0, 1), (0, 1), 14), ("RIGHTPADDING", (0, 1), (0, 1), 14),
    ]))
    story.append(box)
    story.append(Spacer(1, 2.5 * mm))

    # ── CONDITIONS (barre latérale) ──
    story.append(_eyebrow("CONDITIONS"))
    story.append(Spacer(1, 2 * mm))
    cond_txt = (str(settings.footer_note) if settings and settings.footer_note
                else ("Paiement à réception de la facture."
                      + (f" Devis valable jusqu'au {d_valid}." if is_devis and d_valid else "")))
    cond = Table([[Paragraph(cond_txt.replace("\n", "<br/>"), st_muted)]], colWidths=[CONTENT_W])
    cond.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CREAM),
        ("LINEBEFORE", (0, 0), (0, -1), 3, MARINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 16), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    story.append(cond)
    story.append(Spacer(1, 3 * mm))

    # ── Bloc bas : signature (DEVIS) ou simple cachet (FACTURE) ──
    # Sur une FACTURE, rien à signer par le client (pas de "Bon pour accord", pas
    # de QR de signature) : on n'affiche que le cachet (sans titre de section
    # redondant, le label "CACHET & SIGNATURE" sous l'image suffit).
    if is_devis:
        story.append(_eyebrow("SIGNATURES"))
        story.append(Spacer(1, 2.5 * mm))

    stamp_inner = [[Paragraph(f"<font size=8.5 color='#021A3D'><b>CACHET &amp; SIGNATURE - "
                              f"{brand_name.upper()}</b></font>", st_label)]]
    if stamp_path:
        try:
            stamp_inner.append([RLImage(stamp_path, width=44 * mm, height=18 * mm, kind="proportional")])
        except Exception:
            pass
    stamp_c = Table(stamp_inner, colWidths=[CONTENT_W * 0.38])
    stamp_c.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (0, 0), 0), ("BOTTOMPADDING", (0, 0), (0, 0), 8),
    ]))

    if is_devis:
        # Cadre "Bon pour accord" à gauche (à signer par le client) + cachet à droite.
        accord = Table(
            [[Paragraph("<b>BON POUR ACCORD - CLIENT</b>", st_label)],
             [Paragraph("Nom, date &amp; signature précédés de « Lu et approuvé »", st_muted)],
             [Spacer(1, 13 * mm)]],
            colWidths=[CONTENT_W * 0.58],
        )
        accord.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 1, HAIRLINE), ("ROUNDEDCORNERS", [6, 6, 6, 6]),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (0, 0), 10), ("BOTTOMPADDING", (0, 0), (0, 0), 3),
            ("TOPPADDING", (0, 1), (0, 1), 0), ("BOTTOMPADDING", (0, 2), (0, 2), 4),
        ]))
        sign = Table([[accord, stamp_c]], colWidths=[CONTENT_W * 0.60, CONTENT_W * 0.40])
        sign.setStyle(TableStyle([
            ("VALIGN", (0, 0), (0, 0), "TOP"), ("VALIGN", (1, 0), (1, 0), "TOP"),
            ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 4 * mm),
            ("LEFTPADDING", (1, 0), (1, 0), 4 * mm), ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ]))
        story.append(sign)
    else:
        # Facture : cachet seul, aligné à droite.
        stamp_c.hAlign = "RIGHT"
        story.append(stamp_c)

    story.append(Spacer(1, 3 * mm))

    # ── QR "signer en ligne" : uniquement sur le DEVIS (une facture ne se signe pas) ──
    if is_devis:
        try:
            import qrcode
            qr_img = qrcode.make(f"{brand_name} - {title} {number}")
            from io import BytesIO as _BIO
            _qb = _BIO(); qr_img.save(_qb, format="PNG"); _qb.seek(0)
            qr_cell = RLImage(_qb, width=18 * mm, height=18 * mm)
        except Exception:
            qr_cell = Paragraph("", st_muted)
        qr_txt = Paragraph(
            "<font size=11 color='#021A3D'><b>Signature électronique</b></font><br/>"
            "Scannez ce QR code pour <b>consulter et signer ce devis en ligne</b>. "
            "<font size=9 color='#021A3D'>Signature sécurisée - valeur juridique.</font>",
            st_muted)
        qr_row = Table([[qr_cell, qr_txt]], colWidths=[22 * mm, CONTENT_W - 22 * mm])
        qr_row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEABOVE", (0, 0), (-1, 0), 1, HAIRLINE),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (0, 0), (0, 0), 0), ("LEFTPADDING", (1, 0), (1, 0), 8),
        ]))
        story.append(qr_row)

    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    buffer.seek(0)
    return buffer
