"""Quotes & Invoices module - full CRUD, line items, calculations, PDF, payments.

Two blueprints:
  - quotes_bp   (/quotes)   : quotes (creation with line items, PDF, conversion to invoice)
  - invoices_bp (/invoices) : invoices (detail, PDF, payments, deletion)

Security (OWASP access control):
- @login_required + @subscription_required on ALL routes.
- Every quote / invoice belongs to a manager: systematic filtering by
  user_id = current_user.id (a manager NEVER sees another's data).
- CSRF handled globally by Flask-WTF (CSRFProtect) - token rendered in the forms.
- Server-side input validation (owned customer, valid line items, amounts > 0).
- Important actions logged via log_action (MongoDB, best-effort).

Business rule: Senegal VAT 18% (configurable via TAX_RATE), amounts in FCFA.
"""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from io import BytesIO

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    Response, current_app,
)
from flask_login import login_required, current_user
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func

from app.extensions import db
from app.models.customer import Customer
from app.models.quote import Quote, QuoteItem, Invoice, InvoiceItem, Payment
from app.models.settings import CompanySettings
from app.services.activity_log_service import log_action
from app.utils.access import subscription_required

quotes_bp = Blueprint("quotes", __name__, url_prefix="/quotes")
invoices_bp = Blueprint("invoices", __name__, url_prefix="/invoices")

# Senegal VAT rate (configurable). 0.18 = 18%.
TAX_RATE = Decimal("0.18")

# Labels + badge colors per status - STRICT BRAND PALETTE (3 colors).
# Format: (label, background, text). Gold/pale yellow = background, text always navy.
#   - positive (accepted / paid)  → pale yellow
#   - in progress (sent/partial)  → gold background (brand)
#   - negative (refused/unpaid)   → white background + navy border
#   - draft                       → white background + navy border
QUOTE_STATUSES = {
    "draft": ("Brouillon", "#FFFFFF", "#021A3D"),
    "sent": ("Envoyé", "#F2B10E", "#021A3D"),
    "accepted": ("Accepté", "#E8E7A2", "#021A3D"),
    "refused": ("Refusé", "#FFFFFF", "#021A3D"),
}
INVOICE_STATUSES = {
    "unpaid": ("Impayée", "#FFFFFF", "#021A3D"),
    "partial": ("Partielle", "#F2B10E", "#021A3D"),
    "paid": ("Payée", "#E8E7A2", "#021A3D"),
}
PAYMENT_METHODS = {
    "cash": "Espèces",
    "transfer": "Virement",
    "wave": "Wave",
    "orange_money": "Orange Money",
    "check": "Chèque",
}


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------
def _parse_decimal(raw, allow_zero=False):
    """Validate a decimal number. Returns Decimal(2) or None if invalid."""
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
    """Parse a YYYY-MM-DD date; returns today if empty, None if invalid."""
    if raw:
        try:
            return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return date.today()


def _owned_customer_or_none(customer_id):
    """Customer owned by the current manager, else None."""
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
    """Generate a readable number: {prefix}-{year}-{zero-padded counter}.

    The counter is based on the current manager's record count for the
    current year + 1, with an anti-collision guard on uniqueness.
    """
    year = date.today().year
    base = f"{prefix}-{year}-"
    count = model.query.filter(
        model.user_id == current_user.id,
        model.number.like(f"{base}%"),
    ).count()
    seq = count + 1
    # anti-collision guard (globally unique number in the database)
    while model.query.filter_by(number=f"{base}{seq:04d}").first() is not None:
        seq += 1
    return f"{base}{seq:04d}"


def _read_lines():
    """Read the form line items (designation[], quantity[], unit_price[]).

    Returns (items, errors) where items is a list of dicts ready to persist
    and errors a list of messages. Fully empty lines are ignored.
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

        # fully empty line → ignored
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
    """Compute (excl. tax, incl. tax) from a list of items (dicts with 'amount')."""
    excl = sum((it["amount"] for it in items), Decimal("0")).quantize(Decimal("0.01"))
    incl = (excl * (Decimal("1") + TAX_RATE)).quantize(Decimal("0.01"))
    return excl, incl


def _paid_amount(invoice):
    """Sum of an invoice's recorded payments (Decimal)."""
    total = db.session.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.invoice_id == invoice.id
    ).scalar()
    return Decimal(str(total or 0)).quantize(Decimal("0.01"))


def _refresh_invoice_status(invoice):
    """Recompute an invoice's status from the sum of its payments."""
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
    """Format an FCFA amount without decimals, space as thousands separator."""
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
    """Integer -> words (French). Sufficient for FCFA amounts."""
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
    """FCFA amount spelled out in words, capitalized."""
    try:
        n = int(Decimal(str(value or 0)))
    except (InvalidOperation, ValueError):
        n = 0
    words = _n2w(n)
    return (words[0].upper() + words[1:]) + " francs CFA"


# ------------------------------------------------------------
#  QUOTES
# ------------------------------------------------------------
@quotes_bp.route("/")
@login_required
@subscription_required
def index():
    """List the manager's quotes (status, customer, total incl. tax, date)."""
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
    """Quote creation form."""
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
    """Voice-command quote dictation page (Web Speech API)."""
    return render_template("quotes/voice.html")


@quotes_bp.route("/voice/transcribe", methods=["POST"])
@login_required
@subscription_required
def voice_transcribe():
    """Receive audio (MediaRecorder), transcribe it (Whisper), return the text as JSON.

    Compatible with all browsers (Safari included): audio recording is
    universal, unlike the Web Speech API which is Chrome-only.
    """
    from flask import jsonify
    from app.services.transcription_service import transcribe_audio

    audio = request.files.get("audio")
    if audio is None or not audio.filename:
        return jsonify({"ok": False, "error": "Aucun audio reçu."}), 400

    data = audio.read()
    # Size safety net (the global MAX_CONTENT_LENGTH already handles it, but with a clear message).
    result = transcribe_audio(data, filename=audio.filename)
    log_action(current_user.id, "voice_transcribe",
               {"ok": result.get("ok"), "bytes": len(data)})
    return jsonify(result), (200 if result.get("ok") else 422)


@quotes_bp.route("/voice", methods=["POST"])
@login_required
@subscription_required
def voice_parse():
    """Receive the dictated text, interpret it via Claude, pre-fill the form."""
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

    # Match the customer by name (case-insensitive) among the manager's customers.
    matched_id = None
    customer_name = (result.get("customer_name") or "").strip()
    wanted = customer_name.lower()
    for c in customers:
        if c.name.strip().lower() == wanted:
            matched_id = c.id
            break

    # Customer not found but a name was dictated → create it automatically.
    created_client = False
    if not matched_id and customer_name:
        new_customer = Customer(user_id=current_user.id, name=customer_name[:100])
        db.session.add(new_customer)
        db.session.commit()
        matched_id = new_customer.id
        created_client = True
        log_action(current_user.id, "create_customer",
                   {"customer_id": new_customer.id, "name": new_customer.name,
                    "source": "voice_quote"})
        # reload the list so the select displays it
        customers = (Customer.query.filter_by(user_id=current_user.id)
                     .order_by(Customer.name).all())

    # Build a MultiDict to pre-fill the form (like a replayed POST).
    form = MultiDict()
    if matched_id:
        form["customer_id"] = str(matched_id)
    form["quote_date"] = date.today().isoformat()
    for it in result["items"]:
        form.add("designation[]", it["description"])
        form.add("quantity[]", str(it["quantity"]))
        form.add("unit_price[]", str(it["unit_price"]))

    note = "Devis pré-rempli par commande vocale : vérifiez le client, les articles et les prix."
    if created_client:
        note += f" Nouveau client « {customer_name} » ajouté à votre carnet."
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
    """Save a new quote with its line items (server-side validation)."""
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
    """Quote detail (line items + totals)."""
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
    """Quote edit form."""
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
    """Update a quote and replace its line items."""
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
    # replace the line items (delete-orphan cascade)
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
    """Delete a quote owned by the current manager."""
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


def _public_quote_serializer():
    """Signed-token serializer for the public quote link (QR code).

    Stateless by design: the token is an HMAC-signed quote id, so no DB
    column or migration is needed and the link cannot be forged/guessed.
    """
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="quote-public-view")


def _quote_public_url(quote_id):
    token = _public_quote_serializer().dumps(quote_id)
    return url_for("quotes.public_view", token=token, _external=True, _scheme="https")


@quotes_bp.route("/<int:quote_id>/pdf")
@login_required
@subscription_required
def pdf(quote_id):
    """Quote PDF (reportlab)."""
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
        public_url=_quote_public_url(quote.id),
    )
    return Response(
        buffer.getvalue(),
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{quote.number}.pdf"',
        },
    )


def _quote_from_public_token(token):
    """Resolve a signed public token to its quote, or 404."""
    try:
        quote_id = _public_quote_serializer().loads(token)
    except BadSignature:
        abort(404)
    return Quote.query.get_or_404(quote_id)


@quotes_bp.route("/public/<token>")
def public_view(token):
    """Public consultation page of a quote via its signed QR link.

    No login on purpose: the bearer of the signed token (printed as a QR
    code on the PDF) can view this quote only - the token is bound to a
    single quote id and signed with the server secret (OWASP: no IDOR,
    ids are never exposed unsigned). The client can sign the quote
    electronically from this page ("Bon pour accord").
    """
    quote = _quote_from_public_token(token)
    settings = CompanySettings.query.filter_by(user_id=quote.user_id).first()
    log_action(quote.user_id, "public_quote_view", {"quote_id": quote.id, "number": quote.number})
    return render_template(
        "quotes/public_view.html",
        quote=quote, settings=settings, token=token, fmt=_fmt,
    )


@quotes_bp.route("/public/<token>/sign", methods=["POST"])
def public_sign(token):
    """Electronic signature of the quote by the client ("Bon pour accord").

    Simple e-signature: the signer is authenticated by bearing the signed
    link, and the proof (typed name, timestamp, IP) is recorded in the
    activity log (MongoDB). Business-wise the quote becomes `accepted` -
    an existing status, so no schema change.
    """
    quote = _quote_from_public_token(token)
    if quote.status == "accepted":
        flash("Ce devis est déjà signé.", "info")
        return redirect(url_for("quotes.public_view", token=token))

    signer_name = (request.form.get("signer_name") or "").strip()
    approved = request.form.get("approved") == "on"
    if len(signer_name) < 3 or not approved:
        flash("Indiquez votre nom complet et cochez « Lu et approuvé ».", "warning")
        return redirect(url_for("quotes.public_view", token=token))

    quote.status = "accepted"
    db.session.commit()
    signed_at = datetime.utcnow()
    log_action(quote.user_id, "public_quote_signed", {
        "quote_id": quote.id,
        "number": quote.number,
        "signer_name": signer_name[:120],
        "signed_at": signed_at.isoformat() + "Z",
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
    })

    # Notify the manager by email (best-effort: the signature stays valid
    # even if the SMTP send fails - it is already recorded above).
    notified = False
    try:
        from markupsafe import escape
        from app.models.user import User
        from app.services.email_service import send_email
        manager = User.query.get(quote.user_id)
        if manager and manager.email:
            customer_name = quote.customer.name if quote.customer else "-"
            when = signed_at.strftime("%d/%m/%Y à %H:%M UTC")
            subject = f"SenGestion - Devis {quote.number} signé par votre client"
            body = (
                f"Bonjour {manager.name},\n\n"
                f"Bonne nouvelle : votre devis {quote.number} ({customer_name}) "
                f"vient d'être signé électroniquement.\n\n"
                f"Signataire : {signer_name}\n"
                f"Date : {when}\n\n"
                f"Le devis est passé au statut « Accepté » dans votre espace. "
                f"Vous pouvez le convertir en facture depuis la liste des devis.\n\n"
                f"- L'équipe SenGestion"
            )
            html_body = (
                f"<p><strong>Bonne nouvelle :</strong> votre devis "
                f"<strong>{escape(quote.number)}</strong> "
                f"({escape(customer_name)}) vient d'être signé électroniquement.</p>"
                f"<p><strong>Signataire :</strong> {escape(signer_name)}<br>"
                f"<strong>Date :</strong> {when}</p>"
                f"<p>Le devis est passé au statut « Accepté » dans votre espace. "
                f"Vous pouvez le convertir en facture depuis la liste des devis.</p>"
            )
            notified = send_email(manager.email, subject, body, html_body)
    except Exception:
        current_app.logger.exception("Notification signature devis échouée")

    if notified:
        flash("Devis signé - merci ! Le prestataire a été notifié de votre accord.", "success")
    else:
        flash("Devis signé - merci ! Votre accord a été enregistré.", "success")
    return redirect(url_for("quotes.public_view", token=token))


@quotes_bp.route("/public/<token>/pdf")
def public_pdf(token):
    """PDF of the quote, reachable from the public consultation page."""
    quote = _quote_from_public_token(token)
    settings = CompanySettings.query.filter_by(user_id=quote.user_id).first()
    buffer = _build_document_pdf(
        title="DEVIS",
        number=quote.number,
        doc_date=quote.quote_date,
        customer=quote.customer,
        items=quote.items,
        amount_excl=quote.amount_excl_tax,
        amount_incl=quote.amount_incl_tax,
        settings=settings,
        public_url=_quote_public_url(quote.id),
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
    """Convert a quote into an invoice (creates Invoice + InvoiceItems, links invoice_id)."""
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
    db.session.flush()  # obtains invoice.id
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


# ------------------------------------------------------------
#  INVOICES
# ------------------------------------------------------------
@invoices_bp.route("/new")
@login_required
@subscription_required
def new():
    """Direct invoice creation form."""
    customers = Customer.query.filter_by(
        user_id=current_user.id
    ).order_by(Customer.name).all()
    return render_template(
        "invoices/form.html",
        invoice=None, customers=customers,
        tax_rate=TAX_RATE, today=date.today().isoformat(),
    )


@invoices_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def create():
    """Save a new invoice with its line items (server-side validation)."""
    customer_id = request.form.get("customer_id", type=int)
    invoice_date = _parse_date(request.form.get("invoice_date"))

    customer = _owned_customer_or_none(customer_id)
    items, errors = _read_lines()
    if customer is None:
        errors.insert(0, "Sélectionnez un client valide.")
    if invoice_date is None:
        errors.append("La date de la facture est invalide.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        customers = Customer.query.filter_by(user_id=current_user.id).order_by(Customer.name).all()
        return render_template("invoices/form.html", invoice=None, customers=customers,
                               tax_rate=TAX_RATE, today=date.today().isoformat(),
                               form=request.form), 400

    excl, incl = _totals(items)
    invoice = Invoice(
        user_id=current_user.id,
        customer_id=customer.id,
        number=_next_number("FAC", Invoice),
        invoice_date=invoice_date,
        status="unpaid",
        amount_excl_tax=excl,
        amount_incl_tax=incl,
    )
    for it in items:
        invoice.items.append(InvoiceItem(**it))
    db.session.add(invoice)
    db.session.commit()
    log_action(current_user.id, "create_invoice",
               {"invoice_id": invoice.id, "number": invoice.number, "amount_incl_tax": str(incl)})
    flash(f"Facture {invoice.number} créée avec succès.", "success")
    return redirect(url_for("invoices.show", invoice_id=invoice.id))


@invoices_bp.route("/voice")
@login_required
@subscription_required
def voice():
    """Voice-command invoice dictation page."""
    return render_template("invoices/voice.html")


@invoices_bp.route("/voice/transcribe", methods=["POST"])
@login_required
@subscription_required
def voice_transcribe():
    """Transcribe the audio (Whisper) and return the text as JSON (same as quotes)."""
    from flask import jsonify
    from app.services.transcription_service import transcribe_audio
    audio = request.files.get("audio")
    if audio is None or not audio.filename:
        return jsonify({"ok": False, "error": "Aucun audio reçu."}), 400
    data = audio.read()
    result = transcribe_audio(data, filename=audio.filename)
    log_action(current_user.id, "voice_transcribe", {"ok": result.get("ok"), "bytes": len(data)})
    return jsonify(result), (200 if result.get("ok") else 422)


@invoices_bp.route("/voice", methods=["POST"])
@login_required
@subscription_required
def voice_parse():
    """Interpret the dictated text (Claude) and pre-fill the invoice form."""
    from app.services.voice_quote_service import parse_voice_quote
    from werkzeug.datastructures import MultiDict

    transcript = (request.form.get("transcript") or "").strip()
    customers = Customer.query.filter_by(user_id=current_user.id).order_by(Customer.name).all()
    result = parse_voice_quote(transcript, [c.name for c in customers])
    log_action(current_user.id, "voice_invoice", {"ok": result.get("ok"), "len": len(transcript)})

    if not result.get("ok"):
        flash(result.get("error", "Commande vocale non comprise."), "danger")
        return redirect(url_for("invoices.voice"))

    # Match / automatic creation of the customer (same as the voice quote).
    matched_id = None
    customer_name = (result.get("customer_name") or "").strip()
    wanted = customer_name.lower()
    for c in customers:
        if c.name.strip().lower() == wanted:
            matched_id = c.id
            break
    created_client = False
    if not matched_id and customer_name:
        nc = Customer(user_id=current_user.id, name=customer_name[:100])
        db.session.add(nc); db.session.commit()
        matched_id = nc.id
        created_client = True
        log_action(current_user.id, "create_customer",
                   {"customer_id": nc.id, "name": nc.name, "source": "voice_invoice"})
        customers = (Customer.query.filter_by(user_id=current_user.id)
                     .order_by(Customer.name).all())

    form = MultiDict()
    if matched_id:
        form["customer_id"] = str(matched_id)
    form["invoice_date"] = date.today().isoformat()
    for it in result["items"]:
        form.add("designation[]", it["description"])
        form.add("quantity[]", str(it["quantity"]))
        form.add("unit_price[]", str(it["unit_price"]))

    note = "Facture pré-remplie par commande vocale : vérifiez le client, les articles et les prix."
    if created_client:
        note += f" Nouveau client « {customer_name} » ajouté à votre carnet."
    flash(note, "success")
    return render_template("invoices/form.html", invoice=None, customers=customers,
                           tax_rate=TAX_RATE, today=date.today().isoformat(), form=form)


@invoices_bp.route("/")
@login_required
@subscription_required
def index():
    """List the manager's invoices (payment status, customer, total incl. tax, date)."""
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
    """Invoice detail (line items, totals, payments, balance)."""
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
    """Invoice PDF (reportlab)."""
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
    """Record a payment and recompute the invoice status."""
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

    # do not exceed the remaining balance
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
    """Delete a payment and recompute the status."""
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
    """Delete an invoice (and detach the source quote if any)."""
    invoice = _owned_invoice_or_404(invoice_id)
    number = invoice.number

    # detach the source quote to avoid an orphaned FK
    source_quote = Quote.query.filter_by(
        invoice_id=invoice.id, user_id=current_user.id
    ).first()
    if source_quote:
        source_quote.invoice_id = None

    # delete the linked payments, then the invoice
    Payment.query.filter_by(invoice_id=invoice.id).delete()
    db.session.delete(invoice)
    db.session.commit()

    log_action(current_user.id, "delete_invoice", {
        "invoice_id": invoice_id, "number": number,
    })
    flash(f"Facture {number} supprimée.", "success")
    return redirect(url_for("invoices.index"))


# ------------------------------------------------------------
#  PDF generation (reportlab)
# ------------------------------------------------------------
def _build_document_pdf(title, number, doc_date, customer, items,
                        amount_excl, amount_incl, paid=None, due=None,
                        settings=None, public_url=None):
    """Build a PDF (quote or invoice) and return a BytesIO.

    Company header (logo + name + details from settings, otherwise SenGestion),
    customer details, line-items table, excl./VAT/incl. totals, stamp/signature.
    Brand palette: navy #021A3D, gold #F2B10E.
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

    # -- Title font: Palatino (brand). Fallback to Times if unavailable. --
    # Try the font bundled in the project first, then the system one.
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
        # Fallback: Times (built-in serif, close to Palatino - cf. CSS fallback)
        return "Times-Roman", "Times-Bold"

    FONT_TITLE, FONT_TITLE_BOLD = _register_palatino()

    # -- Body font: Arial (UI brand). Fallback to Helvetica if unavailable. --
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
        # Fallback: Helvetica (Arial clone, the brand's official fallback)
        return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"

    FONT_BODY, FONT_BODY_BOLD, FONT_BODY_ITALIC = _register_arial()

    # -- Palette - STRICT BRAND PALETTE, 3 colors (navy / gold / pale yellow) --
    # Neutrals are shades DERIVED from navy (not pure grays), to stay in the
    # brand spirit like the rest of the app (rgba(2,26,61,...)).
    MARINE = colors.HexColor("#021A3D")
    OR = colors.HexColor("#F2B10E")
    JAUNE_PALE = colors.HexColor("#E8E7A2")
    INK_SOFT = MARINE                          # secondary text = NAVY (brand, unambiguous)
    INK_FAINT = MARINE                         # labels = NAVY (brand)
    # Neutrals = white + diluted navy (neutral surfaces, not brand colors)
    ROW_ALT = colors.Color(2/255, 26/255, 61/255, 0.04)    # zebra striping: navy 4%
    HAIRLINE = colors.Color(2/255, 26/255, 61/255, 0.14)   # navy hairlines 14%
    CREAM = colors.white                       # header background = WHITE (neutral surface)
    OBJ_BG = JAUNE_PALE                        # "Objet" card = pale yellow (brand)
    CARD_BG = colors.Color(2/255, 26/255, 61/255, 0.035)   # card background: navy 3.5%

    PAGE_W, PAGE_H = A4
    ML = MR = 15 * mm
    CONTENT_W = PAGE_W - ML - MR
    HEADER_H = 40 * mm

    def _abs(rel):
        if not rel:
            return None
        path = os.path.join(current_app.static_folder, rel)
        return path if os.path.exists(path) else None

    # -- Issuer data --
    brand_name = (settings.company_name if settings and settings.company_name else "SenGestion")
    addr = str(settings.address).replace("\n", ", ") if settings and settings.address else ""
    phone = settings.phone if settings else ""
    email = settings.email if settings else ""
    website = settings.website if settings else ""
    # Legal identifiers line
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
    # Validity: +30 days for a quote
    d_valid = ""
    if doc_date:
        from datetime import timedelta
        d_valid = (doc_date + timedelta(days=30)).strftime("%d/%m/%Y")
    is_devis = title.strip().upper().startswith("DEV")

    # -- Styles (12 pt body text) --
    st_eyebrow = ParagraphStyle("eb", fontName=FONT_BODY_BOLD, fontSize=9,
                                textColor=INK_SOFT, leading=12)  # simulated letter-spacing
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

    # -- Header (cream background) + footer --
    def _decorate(canvas, doc_):
        canvas.saveState()
        # Cream header background
        canvas.setFillColor(CREAM)
        canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, fill=1, stroke=0)
        # Double navy rule under the header
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
        # Company name (brand title - Palatino, per brand guide)
        canvas.setFillColor(MARINE)
        canvas.setFont(FONT_TITLE_BOLD, 20)          # Palatino 20 pt
        canvas.drawString(ML + 34 * mm, PAGE_H - 15 * mm, brand_name)
        # Contact details
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
        # Legal line (smaller, at the very bottom of the header)
        if legal_line:
            canvas.setFont(FONT_BODY, 8)
            canvas.setFillColor(INK_FAINT)
            canvas.drawString(ML, PAGE_H - HEADER_H + 3 * mm, legal_line[:130])

        # Number block on the right
        canvas.setFillColor(INK_FAINT)
        canvas.setFont(FONT_BODY_BOLD, 9)
        canvas.drawRightString(PAGE_W - MR, PAGE_H - 15 * mm,
                               ("NUMÉRO DE DEVIS" if is_devis else "NUMÉRO DE FACTURE"))
        canvas.setFillColor(MARINE)
        canvas.setFont(FONT_TITLE_BOLD, 24)          # Palatino 24 pt (brand titles)
        canvas.drawRightString(PAGE_W - MR, PAGE_H - 23 * mm, number)
        # small decorative gold dot
        canvas.setFillColor(OR)
        canvas.circle(PAGE_W - MR - 2, PAGE_H - 26.5 * mm, 1.6, fill=1, stroke=0)
        # Dates
        canvas.setFillColor(INK_SOFT)
        canvas.setFont(FONT_BODY, 10)
        canvas.drawRightString(PAGE_W - MR, PAGE_H - 31.5 * mm, f"Émis le {d_emis}")
        if is_devis and d_valid:
            canvas.drawRightString(PAGE_W - MR, PAGE_H - 36 * mm, f"Valide jusqu'au {d_valid}")

        # -- Footer --
        canvas.setFillColor(HAIRLINE)
        canvas.rect(ML, 15 * mm, CONTENT_W, 0.6, fill=1, stroke=0)
        canvas.setFillColor(INK_SOFT)
        canvas.setFont(FONT_BODY, 7.6)
        canvas.drawString(ML, 11 * mm,
                          f"{brand_name} - Montants exprimés en francs CFA.")
        canvas.drawRightString(PAGE_W - MR, 11 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=ML, rightMargin=MR,
        topMargin=HEADER_H + 3 * mm, bottomMargin=11 * mm,
        title=f"{title} {number}",
    )
    story = []

    # -- DESTINATAIRE eyebrow --
    def _eyebrow(text):
        # Clean typographic letter-spacing (charSpace): spaces the letters WITHOUT
        # conflating word spaces. A normal space still separates words properly.
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

    # -- CLIENT card (bordered) | OBJET card (colored background) --
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

    # -- Eyebrow + line-items table --
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

    # -- Boxed totals block (right-aligned) --
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
    # 'Déjà réglé / Reste à payer': only if at least one payment is recorded.
    show_payment_lines = paid is not None and Decimal(str(paid or 0)) > 0
    if show_payment_lines:
        inner_rows.append([Paragraph("Déjà réglé", st_tl), Paragraph(f"{_fmt(paid)} FCFA", st_tv)])
        inner_rows.append([Paragraph("Reste à payer", st_ttcl),
                           Paragraph(f"{_fmt(due)} FCFA", st_ttcv)])
    # Block width = right half of the content (flush with the right edge).
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
        # row 4 = "Reste à payer" (navy background, like the total incl. tax)
        inner_style.append(("BACKGROUND", (0, 4), (-1, 4), MARINE))
        inner_style.append(("TOPPADDING", (0, 4), (-1, 4), 11))
        inner_style.append(("BOTTOMPADDING", (0, 4), (-1, 4), 11))
    inner.setStyle(TableStyle(inner_style))

    words_p = Paragraph(_amount_words(amount_incl), st_words)
    # Box flush right (hAlign RIGHT), width = that of the inner totals.
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

    # -- CONDITIONS (side bar) --
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

    # -- Bottom block: signature (QUOTE) or stamp only (INVOICE) --
    # On an INVOICE, nothing for the customer to sign (no "Bon pour accord", no
    # signature QR): only the stamp is shown (no redundant section title,
    # the "CACHET & SIGNATURE" label under the image is enough).
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
        # "Bon pour accord" frame on the left (to be signed by the customer) + stamp on the right.
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
        # Invoice: stamp only, right-aligned.
        stamp_c.hAlign = "RIGHT"
        story.append(stamp_c)

    story.append(Spacer(1, 3 * mm))

    # -- "Sign online" QR: only on the QUOTE (an invoice is not signed) --
    # The QR encodes the signed public consultation URL (public_view route):
    # the client scans it, reads the quote online and signs it electronically.
    if is_devis and public_url:
        try:
            import qrcode
            # QR aux couleurs de la charte : modules marine #021A3D sur blanc
            # (contraste ~14:1, largement suffisant pour la lecture optique)
            _qr = qrcode.QRCode(box_size=10, border=2)
            _qr.add_data(public_url)
            _qr.make(fit=True)
            qr_img = _qr.make_image(fill_color="#021A3D", back_color="white")
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
