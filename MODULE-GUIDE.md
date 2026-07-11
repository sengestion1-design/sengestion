# Guide de développement d'un module — SenGestion

> À suivre par chaque agent développeur de module. Cohérence = note du jury.

## Contexte technique
- **Flask** (app factory dans `app/__init__.py`), **SQLAlchemy** (MySQL), Jinja2.
- Les **modèles existent déjà** dans `app/models/` (ne PAS les recréer) :
  - `customer.py` : `Customer` (customers), `Contact` (contacts)
  - `quote.py` : `Quote`, `QuoteItem`, `Invoice`, `InvoiceItem`, `Payment`
  - `expense.py` : `Expense`, `ExpenseCategory`
  - `message.py` : `Message`
  - `user.py` : `User` (current_user)
- **Chaque enregistrement appartient à un gérant** : filtrer TOUJOURS par `user_id=current_user.id`
  (règle de gestion + sécurité OWASP contrôle d'accès). Ne jamais laisser un gérant voir les données d'un autre.

## Sécurité (obligatoire)
- Toutes les routes : `@login_required` + `@subscription_required` (depuis `app.utils.access`).
- Formulaires POST : inclure `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`.
- Valider les entrées côté serveur. Requêtes via l'ORM (jamais de SQL brut).
- Journaliser les actions importantes : `from app.services.activity_log_service import log_action` → `log_action(current_user.id, "create_customer", {...})`.

## Layout & design (charte imposée)
- Les templates de module ÉTENDENT `layouts/app.html` :
  ```jinja
  {% extends "layouts/app.html" %}
  {% set active = 'customers' %}   {# identifiant du menu actif #}
  {% block title %}… — SenGestion{% endblock %}
  {% block page %} … {% endblock %}
  ```
- `active` possibles : `dashboard`, `customers`, `quotes`, `expenses`, `reports`, `messages`.
- Charte STRICTE : couleurs marine #021A3D / or #F2B10E (fond) / jaune pâle #E8E7A2 / or-texte #8F6600.
  Titres = Palatino 32px (24pt). Texte courant = Arial **16px (12pt) minimum PARTOUT**. RGAA ≥ 4.5:1.
- Classes CSS dispo : `.card`, `.btn-primary` (marine), `.btn-accent` (or), `.form-input`, `.form-label`,
  `.table`, `.badge`, `.alert-*`, `.empty-state`, `.page-header/.page-title/.page-subtitle`.
- Icônes Lucide : `<i data-lucide="users"></i>`.
- En-tête de page type :
  ```html
  <div class="page-header">
    <div><h1 class="page-title">Titre</h1><p class="page-subtitle" style="font-size:16px;">Sous-titre</p></div>
    <a href="…" class="btn btn-accent">+ Nouveau</a>
  </div>
  ```

## Structure d'un module (CRUD complet)
1. `app/routes/<module>.py` : Blueprint avec routes index (liste), new/create (GET+POST), edit/update (GET+POST), delete (POST), show (détail).
2. Enregistrer le blueprint dans `app/__init__.py`.
3. Mettre à jour le lien du menu dans `layouts/app.html` (remplacer `href="#"` par `url_for(...)`).
4. Templates dans `app/templates/<module>/` : `index.html` (liste + recherche), `form.html` (create/edit), `show.html` (détail) si pertinent.

## Convention numéros (devis/factures)
- Générer un numéro lisible : `DEV-2026-0001`, `FAC-2026-0001` (année + compteur).

## Montants
- FCFA (pas de décimales affichées en général). Utiliser `Numeric(12,2)` en base, formater à l'affichage.
