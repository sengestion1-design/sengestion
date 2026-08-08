"""Génère automatiquement le schéma MERISE (PDF) à partir des modèles SQLAlchemy.

À relancer après toute modification des modèles : le schéma reste ainsi
toujours synchronisé avec le code réel.

Usage : python generate_merise.py
Produit : ../MERISE-SenGestion.pdf  (dans le dossier PROJET FINAL)
"""
import os
import base64

import cairosvg
from weasyprint import HTML
from sqlalchemy import inspect as sqla_inspect

from app import create_app
from app.extensions import db

OUT = os.path.join(os.path.dirname(__file__), "..", "MERISE-SenGestion.pdf")
MARINE = "#021A3D"; OR = "#F2B10E"; JPALE = "#E8E7A2"

# Couleur par "domaine" métier (pour le MCD visuel)
COLORS = {
    "users": MARINE,
    "customers": "#0b5cad", "contacts": "#0b7a3a", "quotes": "#0b5cad",
    "invoices": "#8B2500", "payments": "#145A32",
    "quote_items": "#64748b", "invoice_items": "#64748b",
    "expenses": "#b45309", "expense_categories": "#8F6600",
    "messages": "#6d28d9",
}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def collect_schema():
    """Lit les modèles enregistrés et retourne {table: {cols, pk, fks}}."""
    app = create_app("development")
    schema = {}
    with app.app_context():
        insp = sqla_inspect(db.engine) if False else None  # pas besoin de la vraie DB
        for mapper in db.Model.registry.mappers:
            t = mapper.local_table
            if t is None:
                continue
            cols, pk, fks, types = [], [], {}, {}
            for c in t.columns:
                cols.append(c.name)
                # type SQL lisible (VARCHAR(150), INT, DATE, ENUM…)
                try:
                    types[c.name] = str(c.type)
                except Exception:  # noqa: BLE001
                    types[c.name] = c.type.__class__.__name__
                if c.primary_key:
                    pk.append(c.name)
                if c.foreign_keys:
                    fk = list(c.foreign_keys)[0]
                    # on retient la table cible + si la FK est obligatoire (not null)
                    fks[c.name] = {"table": fk.column.table.name,
                                   "nullable": bool(c.nullable)}
            schema[t.name] = {"cols": cols, "pk": pk, "fks": fks, "types": types}
    return schema


# ---------- SVG : une entité ----------
def entity_svg(x, y, w, name, info):
    fill = COLORS.get(name, MARINE)
    hdr = 36; rh = 22
    fields = info["cols"]
    h = hdr + rh * len(fields) + 8
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="9" fill="#fff" stroke="{fill}" stroke-width="2.5"/>'
    s += f'<rect x="{x}" y="{y}" width="{w}" height="{hdr}" rx="9" fill="{fill}"/>'
    s += f'<rect x="{x}" y="{y+hdr-9}" width="{w}" height="9" fill="{fill}"/>'
    s += f'<text x="{x+w/2}" y="{y+23}" fill="#fff" font-size="16" font-weight="700" text-anchor="middle" font-family="Helvetica">{esc(name)}</text>'
    yy = y + hdr + 17
    for c in fields:
        col, weight, prefix = "#1a2b45", "400", ""
        if c in info["pk"]:
            col, weight, prefix = OR, "700", "PK "
        elif c in info["fks"]:
            col, weight, prefix = "#8F6600", "600", "FK "
        s += f'<text x="{x+12}" y="{yy}" fill="{col}" font-size="12.5" font-weight="{weight}" font-family="Helvetica">{esc(prefix+c)}</text>'
        yy += rh
    return s, h


def build_mcd_png(schema):
    """Positionne les entités par domaine et rend le MCD en PNG."""
    # positions manuelles par domaine (x, y)
    # grille très aérée : 5 colonnes espacées de 300px, lignes de 300px
    # (chaque boîte fait ~185px de large et jusqu'à ~230px de haut)
    layout = {
        # top row: SaaS (users) centered + finances on the right
        "users":              (900, 60),
        "expense_categories": (1800, 60),
        # middle row
        "customers":          (90, 500),
        "expenses":           (1450, 550),
        "messages":           (2100, 550),
        # bottom row: commercial chain
        "contacts":           (90, 1100),
        "quotes":             (610, 1100),
        "invoices":           (1190, 1100),
        "payments":           (1800, 1100),
        # sub-row: line items
        "quote_items":        (610, 1580),
        "invoice_items":      (1190, 1580),
    }
    W = 290
    body = ""
    boxes = {}
    for name, (x, y) in layout.items():
        if name not in schema:
            continue
        svg, h = entity_svg(x, y, W, name, schema[name])
        body += svg
        boxes[name] = (x, y, W, h)

    # relations (à partir des FK réelles)
    # a = entité qui porte la FK (côté "plusieurs") ; b = entité référencée (côté "un")
    def card_a(a, fk_col):
        """Cardinalité côté FK : 1,1 si obligatoire, 0,1 si optionnelle."""
        info = schema.get(a, {}).get("fks", {}).get(fk_col)
        if info is None:
            return "0,1"
        return "0,1" if info["nullable"] else "1,1"

    def card_badge(cx, cy, txt):
        """Pastille de cardinalité, grande et lisible."""
        w, h = 40, 22
        return (f'<rect x="{cx-w/2:.0f}" y="{cy-h/2:.0f}" width="{w}" height="{h}" rx="11" '
                f'fill="#fff" stroke="#8F6600" stroke-width="1.6"/>'
                f'<text x="{cx:.0f}" y="{cy+5:.0f}" fill="#8F6600" font-size="14" '
                f'font-weight="800" text-anchor="middle" font-family="Helvetica">{txt}</text>')

    def edge_point(box, tx, ty):
        """Point où la ligne vers (tx,ty) coupe le bord du rectangle box."""
        x, y, w, h = box
        cx, cy = x + w/2, y + h/2
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return cx, cy
        # échelle pour atteindre le bord
        sx = (w/2) / abs(dx) if dx != 0 else 1e9
        sy = (h/2) / abs(dy) if dy != 0 else 1e9
        sc = min(sx, sy)
        return cx + dx*sc, cy + dy*sc

    def line(a, b, label, fk_col, card_b="0,n"):
        if a not in boxes or b not in boxes:
            return ""
        ba, bb = boxes[a], boxes[b]
        ca_x, ca_y = ba[0]+ba[2]/2, ba[1]+ba[3]/2
        cb_x, cb_y = bb[0]+bb[2]/2, bb[1]+bb[3]/2
        # points sur les bords des deux boîtes
        x1, y1 = edge_point(ba, cb_x, cb_y)
        x2, y2 = edge_point(bb, ca_x, ca_y)
        mx, my = (x1+x2)/2, (y1+y2)/2
        lw = len(label)*7 + 16
        ca = card_a(a, fk_col)
        s = f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="#c9a227" stroke-width="2" opacity="0.7"/>'
        # étiquette de l'association (au milieu du segment)
        s += f'<rect x="{mx-lw/2:.0f}" y="{my-11:.0f}" width="{lw:.0f}" height="22" rx="11" fill="{JPALE}" stroke="#d9c98a" stroke-width="1"/>'
        s += f'<text x="{mx:.0f}" y="{my+4:.0f}" fill="#5c4a00" font-size="12" font-weight="700" text-anchor="middle" font-family="Helvetica">{esc(label)}</text>'
        # cardinalités : juste à côté de chaque bord (18% et 82% le long du segment)
        cax, cay = x1 + (x2-x1)*0.18, y1 + (y2-y1)*0.18
        cbx, cby = x1 + (x2-x1)*0.82, y1 + (y2-y1)*0.82
        s += card_badge(cax, cay, ca)
        s += card_badge(cbx, cby, card_b)
        return s

    # (a, b, label, FK_column_in_a, cardinality_on_b_side)
    rels = [
        ("customers","users","manages","user_id","1,n"),
        ("contacts","customers","converts to","customer_id","0,1"),
        ("quotes","customers","places","customer_id","1,n"),
        ("quote_items","quotes","contains","quote_id","1,n"),
        ("invoices","quotes","becomes","quote_id","0,1"),
        ("invoices","customers","billed to","customer_id","1,n"),
        ("invoice_items","invoices","contains","invoice_id","1,n"),
        ("payments","invoices","receives","invoice_id","1,n"),
        ("expenses","expense_categories","category","category_id","0,n"),
        ("expenses","users","records","user_id","1,n"),
        ("messages","users","sends","user_id","1,n"),
    ]
    # dessiner les lignes AVANT les boîtes pour qu'elles passent dessous
    lines = "".join(line(a, b, l, fk, cb) for a, b, l, fk, cb in rels)
    CW, CH = 2500, 2100
    full = (f'<svg viewBox="0 0 {CW} {CH}" width="{CW}" height="{CH}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{CW}" height="{CH}" fill="#f8fafc"/>{lines}{body}</svg>')
    png = cairosvg.svg2png(bytestring=full.encode("utf-8"),
                           output_width=CW*1.5, output_height=CH*1.5)
    return base64.b64encode(png).decode()


TABLE_ORDER = ["users","customers","contacts","quotes","quote_items","invoices",
               "invoice_items","payments","expense_categories","expenses","messages"]

# English descriptions of each field (data dictionary, Julie style)
DESCRIPTIONS = {
    "users": {
        "id": "Unique identifier for the user",
        "name": "User's full name",
        "company": "Manager's company name",
        "email": "Login email (unique)",
        "phone": "Phone number",
        "password_hash": "Hashed password (scrypt)",
        "role": "admin or manager",
        "email_verified": "True once the email is verified",
        "verification_code": "6-digit email verification code",
        "verification_expires": "Verification code expiry time",
        "status": "Subscription status (trial/active/expired/suspended)",
        "trial_start": "Free trial start date",
        "trial_end": "Free trial end date (start + 15 days)",
        "subscription_start": "Paid subscription start date",
        "subscription_end": "Subscription end date (start + 1 year)",
        "validated_by": "Admin who validated the subscription",
        "created_at": "Account creation timestamp",
    },
    "customers": {
        "id": "Unique identifier for the customer", "user_id": "Owner (manager)",
        "name": "Customer name", "company": "Company name", "email": "Contact email",
        "phone": "Phone number", "address": "Postal address", "created_at": "Creation timestamp",
    },
    "contacts": {
        "id": "Unique identifier for the contact", "user_id": "Owner (manager)",
        "name": "Contact name", "first_name": "First name", "company": "Company",
        "role": "Job title", "email": "Email", "phone": "Phone",
        "status": "Prospect stage (new/in_progress/won/lost)",
        "source": "How captured (business_card_scan/manual)",
        "customer_id": "Linked customer once converted", "created_at": "Creation timestamp",
    },
    "quotes": {
        "id": "Unique identifier for the quote", "user_id": "Owner (manager)",
        "customer_id": "Customer the quote is for", "number": "Quote number (unique)",
        "quote_date": "Quote date", "status": "draft/sent/accepted/refused",
        "amount_excl_tax": "Total amount excluding tax", "amount_incl_tax": "Total amount including tax",
        "invoice_id": "Invoice created from this quote", "created_at": "Creation timestamp",
    },
    "quote_items": {
        "id": "Unique identifier for the line", "quote_id": "Parent quote",
        "description": "Item description", "quantity": "Quantity",
        "unit_price": "Unit price", "amount": "Line total",
    },
    "invoices": {
        "id": "Unique identifier for the invoice", "user_id": "Owner (manager)",
        "customer_id": "Customer billed", "quote_id": "Source quote (if any)",
        "number": "Invoice number (unique)", "invoice_date": "Invoice date",
        "status": "unpaid/partial/paid", "amount_excl_tax": "Total excluding tax",
        "amount_incl_tax": "Total including tax", "created_at": "Creation timestamp",
    },
    "invoice_items": {
        "id": "Unique identifier for the line", "invoice_id": "Parent invoice",
        "description": "Item description", "quantity": "Quantity",
        "unit_price": "Unit price", "amount": "Line total",
    },
    "payments": {
        "id": "Unique identifier for the payment", "invoice_id": "Invoice paid",
        "amount": "Paid amount", "payment_date": "Payment date",
        "method": "cash/transfer/wave/orange_money/check",
    },
    "expense_categories": {
        "id": "Unique identifier for the category", "name": "Category name",
    },
    "expenses": {
        "id": "Unique identifier for the expense", "user_id": "Owner (manager)",
        "category_id": "Expense category", "label": "Expense label",
        "amount": "Amount", "expense_date": "Expense date",
        "receipt_ref": "Receipt file reference (scanned)", "created_at": "Creation timestamp",
    },
    "messages": {
        "id": "Unique identifier for the message", "user_id": "Owner (manager)",
        "contact_id": "Recipient contact", "customer_id": "Recipient customer",
        "channel": "Channel (email)", "subject": "Message subject",
        "body": "Message body", "status": "draft/sent/failed",
        "sent_at": "Sent timestamp", "created_at": "Creation timestamp",
    },
}


# Entités (description) - en français, comme Julie
ENTITIES = [
    ("Utilisateur (users)", "gérants abonnés et administrateurs (nom, entreprise, e-mail, rôle, statut d'abonnement, vérification e-mail)."),
    ("Client (customers)", "clients facturables d'un gérant (nom, entreprise, coordonnées, adresse)."),
    ("Contact (contacts)", "prospects captés (scan de carte ou saisie), avec un statut de pipeline ; convertibles en clients."),
    ("Devis (quotes)", "propositions commerciales adressées à un client (numéro, date, statut, montants HT/TTC)."),
    ("Ligne de devis (quote_items)", "détail d'un devis : désignation, quantité, prix unitaire, montant."),
    ("Facture (invoices)", "documents de facturation, souvent issus d'un devis (numéro, date, statut, montants)."),
    ("Ligne de facture (invoice_items)", "détail d'une facture : désignation, quantité, prix unitaire, montant."),
    ("Paiement (payments)", "règlements (total ou partiel) d'une facture (montant, date, moyen)."),
    ("Catégorie de dépense (expense_categories)", "classification des charges de l'entreprise."),
    ("Dépense (expenses)", "charges enregistrées avec catégorie, montant, date et justificatif éventuel."),
    ("Message (messages)", "e-mails de relance envoyés aux contacts ou clients (sujet, corps, statut)."),
]

# Associations (phrases avec cardinalités) - en français, comme Julie
ASSOCIATIONS = [
    "Un gérant (utilisateur) possède 0 à plusieurs clients, contacts, devis, factures et dépenses ; chacun appartient à un seul gérant.",
    "Un contact peut être converti en 1 client (ou aucun) ; un client provient d'au plus un contact.",
    "Un client passe 0 à plusieurs devis, mais chaque devis appartient à un seul client.",
    "Un devis contient 1 à plusieurs lignes ; chaque ligne appartient à un seul devis.",
    "Un devis peut se transformer en une seule facture ; une facture provient d'au plus un devis.",
    "Une facture est liée à un seul client et contient 1 à plusieurs lignes de facture.",
    "Une facture reçoit 1 à plusieurs paiements ; chaque paiement est lié à une seule facture.",
    "Une dépense appartient à une seule catégorie ; une catégorie regroupe 0 à plusieurs dépenses.",
    "Seuls les administrateurs peuvent valider les abonnements et gérer les comptes des gérants.",
]

# Règles de gestion (business rules) - en français, comme Julie
BUSINESS_RULES = [
    ("Comptes &amp; abonnement", [
        "Un utilisateur doit vérifier son e-mail (code à 6 chiffres) avant de pouvoir se connecter.",
        "Chaque nouveau gérant bénéficie d'un essai gratuit de 15 jours, puis d'un abonnement annuel validé par l'administrateur.",
        "La validation des abonnements et la gestion des comptes sont réservées exclusivement aux administrateurs.",
    ]),
    ("Clients &amp; prospects", [
        "Chaque client, contact, devis, facture et dépense appartient à un et un seul gérant (utilisateur).",
        "Un contact (prospect) peut être converti en client ; un client ne peut pas redevenir un simple contact.",
    ]),
    ("Devis &amp; factures", [
        "Chaque devis et chaque facture doit être rattaché à un client existant.",
        "Une facture peut être issue d'un devis ; un devis ne donne lieu qu'à une seule facture.",
        "Un devis ou une facture est composé d'au moins une ligne (produit ou service).",
        "Une facture peut recevoir plusieurs paiements (partiels) ; son statut passe à « payée » lorsque le total est réglé.",
    ]),
    ("Dépenses", [
        "Chaque dépense appartient à une catégorie et peut être accompagnée d'un justificatif (fichier scanné).",
    ]),
]


def build_business_rules():
    html = ""
    for group, rules in BUSINESS_RULES:
        items = "".join(f"<li>{r}</li>" for r in rules)
        html += f'<div class="rulegrp"><div class="rulehead">{group}</div><ul>{items}</ul></div>'
    return html


def build_entities():
    items = "".join(f"<li><b>{esc(n)}</b> : {esc(d)}</li>" for n, d in ENTITIES)
    return f'<ul class="ent">{items}</ul>'


def build_associations():
    items = "".join(f"<li>{esc(a)}</li>" for a in ASSOCIATIONS)
    return f'<ul class="assoc">{items}</ul>'


def build_mpd_tables(schema):
    html = ""
    for name in TABLE_ORDER:
        if name not in schema:
            continue
        info = schema[name]
        rows = ""
        for c in info["cols"]:
            key = "PK" if c in info["pk"] else (f"FK {info['fks'][c]['table']}" if c in info["fks"] else "")
            rows += f"<tr><td>{esc(c)}</td><td>{esc(key)}</td></tr>"
        html += (f'<div class="tbl"><div class="tname">{esc(name)}</div>'
                 f'<table><tr><th>Field</th><th>Key</th></tr>{rows}</table></div>')
    return html


def build_data_dictionary(schema):
    """Data dictionary (Julie style): Table / Field / Type / Description."""
    rows = ""
    for name in TABLE_ORDER:
        if name not in schema:
            continue
        info = schema[name]
        descs = DESCRIPTIONS.get(name, {})
        first = True
        for c in info["cols"]:
            typ = info["types"].get(c, "")
            if c in info["pk"]:
                typ += " · PK"
            elif c in info["fks"]:
                typ += f" · FK→{info['fks'][c]['table']}"
            desc = descs.get(c, "")
            tname = f'<b>{esc(name)}</b>' if first else ""
            rows += (f'<tr><td class="dt">{tname}</td><td>{esc(c)}</td>'
                     f'<td class="ty">{esc(typ)}</td><td>{esc(desc)}</td></tr>')
            first = False
        # séparateur léger entre tables
        rows += '<tr class="sep"><td colspan="4"></td></tr>'
    return (f'<table class="dict"><tr><th>Table</th><th>Field</th>'
            f'<th>Type</th><th>Description</th></tr>{rows}</table>')


def main():
    schema = collect_schema()
    mcd_b64 = build_mcd_png(schema)
    rules_html = build_business_rules()
    entities_html = build_entities()
    assoc_html = build_associations()
    tables_html = build_mpd_tables(schema)
    dict_html = build_data_dictionary(schema)
    nb_tables = len(schema)

    DOC = f"""
    <div class="cover">
      <div class="t1">Data Model - MERISE</div>
      <div class="t2">SenGestion · MySQL (CP5) + NoSQL MongoDB (CP6) · {nb_tables} tables · generated from code</div>
    </div>
    <h2>Règles de gestion</h2>
    <p class="lead">Contraintes métier qui régissent le fonctionnement de l'application et guident la conception du modèle.</p>
    <div class="rules">{rules_html}</div>

    <h2>Entités</h2>
    {entities_html}

    <h2>Associations entre les entités</h2>
    {assoc_html}

    <h2>CDM - Schéma conceptuel des données</h2>
    <p class="lead">Entités, associations et cardinalités. <b style="color:{OR}">PK</b> = clé primaire · <b style="color:#8F6600">FK</b> = clé étrangère.</p>
    <div class="fig"><img src="data:image/png;base64,{mcd_b64}" style="width:100%;max-width:800px"/></div>

    <h2>Data dictionary</h2>
    <p class="lead">Every field of the relational schema, with its SQL type and description.</p>
    {dict_html}
    <h2>PDM - Physical Data Model (MySQL)</h2>
    <div class="tables">{tables_html}</div>
    <h2>NoSQL data (MongoDB) - CP6</h2>
    <table class="nosql">
      <tr><th>Collection</th><th>Content</th></tr>
      <tr><td><b>activity_logs</b></td><td>Action log (login, creation…)</td></tr>
      <tr><td><b>scan_results</b></td><td>AI-extracted data (business card, receipt) + score</td></tr>
      <tr><td><b>voice_transcriptions</b></td><td>Voice transcriptions (Whisper) + Claude extraction</td></tr>
    </table>
    <div class="just"><b>Architecture:</b> MySQL for relational data requiring strong integrity
    (quote→customer, invoice→quote, ACID consistency) · MongoDB for semi-structured,
    high-volume or variable-shape data (logs, AI outputs). SQL = structure &amp; reliability · NoSQL = flexibility &amp; volume.</div>
    <div class="footer">Schema automatically generated from the SQLAlchemy models - always in sync with the code.</div>
    """

    CSS = f"""
    @page {{ size:A4; margin:1.3cm 1.3cm; @bottom-center {{ content:"MERISE SenGestion (auto) · " counter(page) "/" counter(pages); font-size:8px; color:#9aa4b2; }} }}
    * {{ box-sizing:border-box; }}
    body {{ font-family:Helvetica,Arial,sans-serif; color:{MARINE}; font-size:9.5px; line-height:1.4; }}
    .cover {{ background:{MARINE}; color:#fff; border-radius:14px; padding:20px 24px; margin-bottom:10px; }}
    .cover .t1 {{ font-size:23px; font-weight:800; }}
    .cover .t2 {{ color:{JPALE}; font-size:11px; margin-top:6px; font-weight:600; }}
    h2 {{ font-size:13px; color:{MARINE}; margin:16px 0 6px; border-bottom:3px solid {OR}; padding-bottom:4px; }}
    .lead {{ color:#4A554F; font-size:9px; margin-bottom:8px; }}
    .fig {{ text-align:center; padding:10px; background:#f8fafc; border:1px solid #e2e6ec; border-radius:12px; page-break-inside:avoid; }}
    .tables {{ display:flex; flex-wrap:wrap; gap:9px; }}
    .tbl {{ width:31.5%; border:1px solid #e2e6ec; border-radius:8px; overflow:hidden; page-break-inside:avoid; }}
    .tname {{ background:{MARINE}; color:#fff; font-weight:700; font-size:10px; padding:4px 8px; }}
    .tbl table {{ width:100%; border-collapse:collapse; font-size:8px; }}
    .tbl th {{ background:#eef1f0; text-align:left; padding:3px 7px; }}
    .tbl td {{ padding:2px 7px; border-bottom:1px solid #f0f2f4; }}
    table.nosql {{ width:100%; border-collapse:separate; border-spacing:0; font-size:9px; border-radius:8px; overflow:hidden; }}
    table.nosql th {{ background:linear-gradient(90deg,#0b7a3a,#13aa52); color:#fff; text-align:left; padding:6px 10px; }}
    table.nosql td {{ padding:5px 10px; border-bottom:1px solid #e5e9f0; }}
    .just {{ background:{JPALE}; border-radius:10px; padding:10px 13px; margin-top:12px; font-size:9px; }}
    .footer {{ margin-top:12px; padding-top:7px; border-top:1px solid #d9dce2; font-size:8px; color:#7a8494; }}
    table.dict {{ width:100%; border-collapse:collapse; font-size:8.5px; }}
    table.dict th {{ background:{MARINE}; color:#fff; text-align:left; padding:5px 8px; }}
    table.dict td {{ padding:3px 8px; border-bottom:1px solid #eef1f0; vertical-align:top; }}
    table.dict .dt {{ color:{MARINE}; white-space:nowrap; }}
    table.dict .ty {{ font-family:Menlo,monospace; font-size:7.5px; color:#8F6600; }}
    table.dict tr.sep td {{ border-bottom:2px solid #d9dce2; padding:1px; }}
    .rules {{ display:flex; flex-wrap:wrap; gap:10px; }}
    .rulegrp {{ width:48.5%; background:#fbfcfe; border:1px solid #e2e6ec; border-left:4px solid {OR};
      border-radius:8px; padding:8px 12px; page-break-inside:avoid; }}
    .rulehead {{ font-weight:800; color:{MARINE}; font-size:11px; margin-bottom:4px; }}
    .rulegrp ul {{ margin:0 0 0 16px; padding:0; }}
    .rulegrp li {{ margin:4px 0; font-size:9.5px; color:#33404f; }}
    ul.ent {{ margin:4px 0 4px 18px; }} ul.ent li {{ margin:4px 0; font-size:10px; color:#33404f; }}
    ul.ent b {{ color:{MARINE}; }}
    ul.assoc {{ margin:4px 0 4px 18px; }} ul.assoc li {{ margin:5px 0; font-size:10px; color:#33404f; }}
    ul.assoc li::marker {{ color:{OR}; }}
    """

    HTML(string=f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{DOC}</body></html>").write_pdf(OUT)
    print(f"✅ MERISE généré depuis le code : {nb_tables} tables → {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
