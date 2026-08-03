"""Contacts & Customers module - CRUD routes.

Two blueprints:
  - customers_bp (/customers) : final customers (customer portfolio)
  - contacts_bp  (/contacts)  : prospects (sales pipeline) + conversion into customer

Security (OWASP access control): every query is scoped to current_user.id so a
manager can never read or alter another manager's records. All POST routes are
CSRF-protected (Flask-WTF global CSRF, token rendered in the forms). Actions are
logged in MongoDB via log_action (best-effort).
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    current_app,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.customer import Customer, Contact
from app.models.quote import Quote, Invoice
from app.services.activity_log_service import log_action
from app.services.business_card_service import scan_business_card
from app.utils.access import subscription_required

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")
contacts_bp = Blueprint("contacts", __name__, url_prefix="/contacts")

# Pipeline labels + badge classes for prospects (Contact.status enum)
CONTACT_STATUSES = {
    "new": ("Nouveau", "badge-info"),
    "in_progress": ("En cours", "badge-warning"),
    "won": ("Gagné", "badge-success"),
    "lost": ("Perdu", "badge-danger"),
}


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------
def _clean(value: str | None, max_len: int) -> str | None:
    """Trim a form field and cap its length; empty string becomes None."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value[:max_len]


def _get_owned_customer(customer_id: int) -> Customer:
    """Fetch a customer owned by the current user, or 404 (access control)."""
    customer = Customer.query.filter_by(
        id=customer_id, user_id=current_user.id
    ).first()
    if customer is None:
        abort(404)
    return customer


def _get_owned_contact(contact_id: int) -> Contact:
    """Fetch a contact owned by the current user, or 404 (access control)."""
    contact = Contact.query.filter_by(
        id=contact_id, user_id=current_user.id
    ).first()
    if contact is None:
        abort(404)
    return contact


# ═════════════════════════════════════════════════════════════════════════════
#  CUSTOMERS  (clients)
# ═════════════════════════════════════════════════════════════════════════════
@customers_bp.route("/")
@login_required
@subscription_required
def index():
    """List the current manager's customers, with a name/company search."""
    q = (request.args.get("q") or "").strip()
    query = Customer.query.filter_by(user_id=current_user.id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Customer.name.ilike(like),
                                    Customer.company.ilike(like)))
    customers = query.order_by(Customer.created_at.desc()).all()
    return render_template("customers/index.html", customers=customers, q=q)


@customers_bp.route("/new")
@login_required
@subscription_required
def new():
    """Display the empty customer creation form."""
    return render_template("customers/form.html", customer=None)


@customers_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def create():
    """Create a new customer owned by the current manager."""
    name = _clean(request.form.get("name"), 100)
    if not name:
        flash("Le nom du client est obligatoire.", "danger")
        return render_template("customers/form.html", customer=None,
                               form=request.form), 400

    customer = Customer(
        user_id=current_user.id,
        name=name,
        company=_clean(request.form.get("company"), 150),
        email=_clean(request.form.get("email"), 150),
        phone=_clean(request.form.get("phone"), 20),
        address=_clean(request.form.get("address"), 2000),
    )
    db.session.add(customer)
    db.session.commit()
    log_action(current_user.id, "create_customer",
               {"customer_id": customer.id, "name": customer.name})
    flash(f"Client « {customer.name} » créé avec succès.", "success")
    return redirect(url_for("customers.show", customer_id=customer.id))


@customers_bp.route("/<int:customer_id>")
@login_required
@subscription_required
def show(customer_id):
    """Customer detail sheet: info + their quotes and invoices."""
    customer = _get_owned_customer(customer_id)
    quotes = (Quote.query
              .filter_by(customer_id=customer.id, user_id=current_user.id)
              .order_by(Quote.created_at.desc()).all())
    invoices = (Invoice.query
                .filter_by(customer_id=customer.id, user_id=current_user.id)
                .order_by(Invoice.created_at.desc()).all())
    return render_template("customers/show.html", customer=customer,
                           quotes=quotes, invoices=invoices)


@customers_bp.route("/<int:customer_id>/edit")
@login_required
@subscription_required
def edit(customer_id):
    """Display the edit form pre-filled with the customer's data."""
    customer = _get_owned_customer(customer_id)
    return render_template("customers/form.html", customer=customer)


@customers_bp.route("/<int:customer_id>", methods=["POST"])
@login_required
@subscription_required
def update(customer_id):
    """Update a customer (owned by the current manager)."""
    customer = _get_owned_customer(customer_id)
    name = _clean(request.form.get("name"), 100)
    if not name:
        flash("Le nom du client est obligatoire.", "danger")
        return render_template("customers/form.html", customer=customer,
                               form=request.form), 400

    customer.name = name
    customer.company = _clean(request.form.get("company"), 150)
    customer.email = _clean(request.form.get("email"), 150)
    customer.phone = _clean(request.form.get("phone"), 20)
    customer.address = _clean(request.form.get("address"), 2000)
    db.session.commit()
    log_action(current_user.id, "update_customer",
               {"customer_id": customer.id, "name": customer.name})
    flash(f"Client « {customer.name} » mis à jour.", "success")
    return redirect(url_for("customers.show", customer_id=customer.id))


@customers_bp.route("/<int:customer_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete(customer_id):
    """Delete a customer (owned by the current manager)."""
    customer = _get_owned_customer(customer_id)
    name = customer.name
    db.session.delete(customer)
    db.session.commit()
    log_action(current_user.id, "delete_customer",
               {"customer_id": customer_id, "name": name})
    flash(f"Client « {name} » supprimé.", "success")
    return redirect(url_for("customers.index"))


# ═════════════════════════════════════════════════════════════════════════════
#  CONTACTS (address book)  &  PROSPECTS (sales pipeline)
#  Same `contacts` table, distinguished by the `kind` column:
#    - kind="contact"  → directory, no active outreach
#    - kind="prospect" → tracked sales lead (pipeline new→won/lost)
#  Journey: Contact → (promotion) → Prospect → (conversion) → Customer.
# ═════════════════════════════════════════════════════════════════════════════
def _contacts_of_kind(kind: str):
    """Query helper: current user's contacts of a given kind + name/company search."""
    q = (request.args.get("q") or "").strip()
    query = Contact.query.filter_by(user_id=current_user.id, kind=kind)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Contact.name.ilike(like),
                                    Contact.company.ilike(like)))
    return query.order_by(Contact.created_at.desc()).all(), q


# -- CONTACTS (address book) --------------------------------------------------
@contacts_bp.route("/")
@login_required
@subscription_required
def index():
    """List the current manager's address-book contacts (kind=contact)."""
    contacts, q = _contacts_of_kind("contact")
    return render_template("contacts/index.html", contacts=contacts, q=q,
                           statuses=CONTACT_STATUSES)


# -- PROSPECTS (pipeline) -----------------------------------------------------
@contacts_bp.route("/prospects")
@login_required
@subscription_required
def prospects():
    """List the current manager's prospects (kind=prospect, sales pipeline)."""
    contacts, q = _contacts_of_kind("prospect")
    return render_template("contacts/prospects.html", contacts=contacts, q=q,
                           statuses=CONTACT_STATUSES)


@contacts_bp.route("/new")
@login_required
@subscription_required
def new():
    """Display the empty creation form. ?kind=prospect for a pipeline lead."""
    kind = "prospect" if request.args.get("kind") == "prospect" else "contact"
    return render_template("contacts/form.html", contact=None,
                           statuses=CONTACT_STATUSES, kind=kind)


@contacts_bp.route("/", methods=["POST"])
@login_required
@subscription_required
def create():
    """Create a new contact or prospect owned by the current manager."""
    kind = "prospect" if request.form.get("kind") == "prospect" else "contact"
    name = _clean(request.form.get("name"), 100)
    if not name:
        flash("Le nom est obligatoire.", "danger")
        return render_template("contacts/form.html", contact=None,
                               statuses=CONTACT_STATUSES, kind=kind,
                               form=request.form), 400

    status = request.form.get("status") or "new"
    if status not in CONTACT_STATUSES:
        status = "new"

    # Origin: scanned card (AI) or manual entry
    source = "business_card_scan" if request.form.get("source") == "business_card_scan" else "manual"

    # Scanned card photo: only keep paths we generated ourselves.
    card_image = request.form.get("card_image") or None
    # Anti-injection / path traversal: only our paths, never "..".
    if card_image and (not card_image.startswith("uploads/cartes/")
                       or ".." in card_image):
        card_image = None

    contact = Contact(
        user_id=current_user.id,
        name=name,
        first_name=_clean(request.form.get("first_name"), 100),
        company=_clean(request.form.get("company"), 150),
        role=_clean(request.form.get("role"), 100),
        email=_clean(request.form.get("email"), 150),
        phone=_clean(request.form.get("phone"), 20),
        kind=kind,
        status=status,
        source=source,
        card_image=card_image,
    )
    db.session.add(contact)
    db.session.commit()
    log_action(current_user.id, "create_contact",
               {"contact_id": contact.id, "name": contact.name,
                "kind": kind, "source": source})
    label = "Prospect" if kind == "prospect" else "Contact"
    flash(f"{label} « {contact.name} » créé avec succès.", "success")
    return redirect(url_for("contacts.prospects" if kind == "prospect" else "contacts.index"))


@contacts_bp.route("/<int:contact_id>/promote", methods=["POST"])
@login_required
@subscription_required
def promote(contact_id):
    """Promote an address-book contact into a sales prospect (kind contact→prospect)."""
    contact = _get_owned_contact(contact_id)
    if contact.kind == "prospect":
        flash("Ce contact est déjà un prospect.", "warning")
        return redirect(url_for("contacts.prospects"))
    contact.kind = "prospect"
    contact.status = "new"          # enters the pipeline
    db.session.commit()
    log_action(current_user.id, "promote_contact",
               {"contact_id": contact.id, "name": contact.name})
    flash(f"« {contact.name} » est passé en prospect.", "success")
    return redirect(url_for("contacts.prospects"))


# ------------------------------------------------------------
#  Business-card scan (AI - Claude Vision)
# ------------------------------------------------------------
# Image extensions allowed at upload (defense in depth).
_ALLOWED_IMG_EXT = {"jpg", "jpeg", "png", "webp", "gif"}


def _normalize_image(raw_bytes: bytes, original_name: str):
    """Convert any image into a JPEG readable by Claude Vision.

    Handles iPhone photos (HEIC), uppercase or missing extensions,
    and varied formats. Returns (jpeg_bytes, filename, "image/jpeg"),
    or (None, None, None) if the file is not a decodable image.
    """
    import io
    from PIL import Image

    # Optional HEIC support (iPhone photos) if pillow-heif is installed.
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
    except Exception:
        pass  # HEIC unavailable → Pillow will raise a clear error below

    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.load()
        # JPEG has no alpha: convert to RGB (flattened on white background if needed).
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90)
        return out.getvalue(), "carte.jpg", "image/jpeg"
    except Exception:
        return None, None, None


def _save_card_image(jpeg_bytes: bytes, user_id: int) -> str | None:
    """Save the card's JPEG under /static/uploads/cartes/ and return its
    relative path (e.g. 'uploads/cartes/u1-ab12cd34.jpg'), or None on failure."""
    import os
    import uuid
    from flask import current_app

    try:
        rel_dir = os.path.join("uploads", "cartes")
        abs_dir = os.path.join(current_app.static_folder, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        fname = f"u{user_id}-{uuid.uuid4().hex[:12]}.jpg"
        with open(os.path.join(abs_dir, fname), "wb") as fh:
            fh.write(jpeg_bytes)
        return f"{rel_dir}/{fname}"          # path relative to /static (web slashes)
    except Exception:
        current_app.logger.warning("Failed to save card image", exc_info=True)
        return None


@contacts_bp.route("/scan")
@login_required
@subscription_required
def scan():
    """Display the upload page for a business card to analyze."""
    return render_template("contacts/scan.html")


@contacts_bp.route("/scan", methods=["POST"])
@login_required
@subscription_required
def scan_upload():
    """Receive the image, call Claude Vision, pre-fill the contact form."""
    file = request.files.get("card")
    if file is None or not file.filename:
        flash("Veuillez sélectionner une image de carte de visite.", "danger")
        return redirect(url_for("contacts.scan"))

    raw_bytes = file.read()
    if not raw_bytes:
        flash("Le fichier reçu est vide. Réessayez.", "danger")
        return redirect(url_for("contacts.scan"))

    # Normalization: EVERY image is converted to JPEG via Pillow.
    # This accepts iPhone photos (HEIC), uppercase extensions,
    # files without an extension, etc. - and guarantees a format readable by the AI.
    image_bytes, filename, content_type = _normalize_image(raw_bytes, file.filename)
    if image_bytes is None:
        flash("Ce fichier n'est pas une image valide. "
              "Utilisez une photo JPG, PNG, WebP ou HEIC.", "danger")
        return redirect(url_for("contacts.scan"))

    result = scan_business_card(image_bytes, filename=filename,
                               content_type=content_type)

    log_action(current_user.id, "scan_business_card",
               {"ok": result.get("ok"), "filename": file.filename})

    if not result.get("ok"):
        flash(result.get("error", "La carte n'a pas pu être analysée."), "danger")
        return redirect(url_for("contacts.scan"))

    # Save the card photo (normalized JPEG) to attach it to the contact.
    card_image = _save_card_image(image_bytes, current_user.id)

    # Success: re-display the contact form, pre-filled, marked "scan".
    # (No flash here: the page's AI banner already carries the verification instruction.)
    fields = result["fields"]
    return render_template(
        "contacts/form.html",
        contact=None,
        statuses=CONTACT_STATUSES,
        kind="contact",            # a scanned card goes into the address book
        form=fields,               # pre-fill
        from_scan=True,            # marks the origin (hidden source field)
        card_image=card_image,     # path of the scanned photo (preview + save)
    )


def _list_endpoint_for(contact) -> str:
    """Return the list route (contacts vs prospects) an item belongs to."""
    return "contacts.prospects" if contact.kind == "prospect" else "contacts.index"


@contacts_bp.route("/<int:contact_id>")
@login_required
@subscription_required
def show(contact_id):
    """Display a read-only detail page for a contact/prospect."""
    contact = _get_owned_contact(contact_id)
    return render_template("contacts/show.html", contact=contact,
                           statuses=CONTACT_STATUSES)


@contacts_bp.route("/<int:contact_id>/edit")
@login_required
@subscription_required
def edit(contact_id):
    """Display the edit form pre-filled with the contact's data."""
    contact = _get_owned_contact(contact_id)
    return render_template("contacts/form.html", contact=contact,
                           statuses=CONTACT_STATUSES, kind=contact.kind)


@contacts_bp.route("/<int:contact_id>", methods=["POST"])
@login_required
@subscription_required
def update(contact_id):
    """Update a contact/prospect (owned by the current manager)."""
    contact = _get_owned_contact(contact_id)
    name = _clean(request.form.get("name"), 100)
    if not name:
        flash("Le nom est obligatoire.", "danger")
        return render_template("contacts/form.html", contact=contact,
                               statuses=CONTACT_STATUSES, kind=contact.kind,
                               form=request.form), 400

    status = request.form.get("status") or "new"
    if status not in CONTACT_STATUSES:
        status = "new"

    contact.name = name
    contact.first_name = _clean(request.form.get("first_name"), 100)
    contact.company = _clean(request.form.get("company"), 150)
    contact.role = _clean(request.form.get("role"), 100)
    contact.email = _clean(request.form.get("email"), 150)
    contact.phone = _clean(request.form.get("phone"), 20)
    contact.status = status
    db.session.commit()
    log_action(current_user.id, "update_contact",
               {"contact_id": contact.id, "name": contact.name})
    label = "Prospect" if contact.kind == "prospect" else "Contact"
    flash(f"{label} « {contact.name} » mis à jour.", "success")
    return redirect(url_for(_list_endpoint_for(contact)))


@contacts_bp.route("/<int:contact_id>/delete", methods=["POST"])
@login_required
@subscription_required
def delete(contact_id):
    """Delete a contact/prospect (owned by the current manager)."""
    contact = _get_owned_contact(contact_id)
    name = contact.name
    endpoint = _list_endpoint_for(contact)
    # Cleanup: also delete the associated card photo (avoids orphans).
    if (contact.card_image and contact.card_image.startswith("uploads/cartes/")
            and ".." not in contact.card_image):
        import os
        try:
            os.remove(os.path.join(current_app.static_folder, contact.card_image))
        except OSError:
            pass
    db.session.delete(contact)
    db.session.commit()
    log_action(current_user.id, "delete_contact",
               {"contact_id": contact_id, "name": name})
    flash(f"« {name} » supprimé.", "success")
    return redirect(url_for(endpoint))


@contacts_bp.route("/<int:contact_id>/convert", methods=["POST"])
@login_required
@subscription_required
def convert(contact_id):
    """Convert a prospect into a customer and link contact.customer_id."""
    contact = _get_owned_contact(contact_id)
    if contact.is_converted:
        flash("Ce prospect a déjà été converti en client.", "warning")
        return redirect(url_for("contacts.prospects"))

    # Build a display name from first name + name when available.
    full_name = " ".join(p for p in [contact.first_name, contact.name] if p)
    customer = Customer(
        user_id=current_user.id,
        name=full_name[:100] or contact.name,
        company=contact.company,
        email=contact.email,
        phone=contact.phone,
    )
    db.session.add(customer)
    db.session.flush()          # obtain customer.id before linking
    contact.customer_id = customer.id
    contact.status = "won"      # a converted prospect is a won deal
    db.session.commit()
    log_action(current_user.id, "convert_contact",
               {"contact_id": contact.id, "customer_id": customer.id})
    flash(f"Contact converti en client « {customer.name} ».", "success")
    return redirect(url_for("customers.show", customer_id=customer.id))
