"""Reports module route (KPIs filterable by period)."""
import csv
import io
from calendar import month_abbr
from datetime import date

from flask import Blueprint, render_template, request, Response
from flask_login import login_required, current_user

from app.extensions import db
from app.models.quote import Quote, Invoice, Payment
from app.models.expense import Expense, ExpenseCategory
from app.utils.access import subscription_required

reports_bp = Blueprint("reports", __name__, url_prefix="/rapports")


def _period_bounds(period, year, month):
    """Date bounds (start, end) for the chosen period."""
    if period == "year":
        return date(year, 1, 1), date(year, 12, 31)
    # month by default
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1)
    return date(year, month, 1), end


def _build_report(user_id, period, year, month):
    start, end = _period_bounds(period, year, month)

    quotes = db.session.query(Quote).filter(
        Quote.user_id == user_id,
        Quote.quote_date >= start, Quote.quote_date <= end,
    )
    invoices = db.session.query(Invoice).filter(
        Invoice.user_id == user_id,
        Invoice.invoice_date >= start, Invoice.invoice_date <= end,
    )

    ca_facture = db.session.query(db.func.coalesce(db.func.sum(Invoice.amount_incl_tax), 0)) \
        .filter(Invoice.user_id == user_id,
                Invoice.invoice_date >= start, Invoice.invoice_date <= end).scalar() or 0

    encaisse = db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)) \
        .join(Invoice, Payment.invoice_id == Invoice.id) \
        .filter(Invoice.user_id == user_id,
                Payment.payment_date >= start, Payment.payment_date <= end).scalar() or 0

    depenses = db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0)) \
        .filter(Expense.user_id == user_id,
                Expense.expense_date >= start, Expense.expense_date <= end).scalar() or 0

    devis_acceptes = quotes.filter(Quote.status == "accepted").count()
    devis_total = quotes.count()
    taux_conversion = round((devis_acceptes / devis_total) * 100) if devis_total else 0

    return {
        "period_start": start,
        "period_end": end,
        "quotes_count": devis_total,
        "quotes_accepted": devis_acceptes,
        "conversion_rate": taux_conversion,
        "invoices_count": invoices.count(),
        "revenue": float(ca_facture),
        "collected": float(encaisse),
        "expenses": float(depenses),
        "net_balance": float(encaisse) - float(depenses),
    }


def _month_range(end_year, end_month, count=6):
    """List of (year, month) for the last `count` months, oldest first."""
    months = []
    y, m = end_year, end_month
    for _ in range(count):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(months))


def _build_trend(user_id, end_year, end_month):
    """Invoiced revenue and expenses over the last 6 months."""
    labels, revenue, expenses = [], [], []
    for y, m in _month_range(end_year, end_month, 6):
        start, end = _period_bounds("month", y, m)

        ca = db.session.query(db.func.coalesce(db.func.sum(Invoice.amount_incl_tax), 0)) \
            .filter(Invoice.user_id == user_id,
                    Invoice.invoice_date >= start, Invoice.invoice_date <= end).scalar() or 0
        dep = db.session.query(db.func.coalesce(db.func.sum(Expense.amount), 0)) \
            .filter(Expense.user_id == user_id,
                    Expense.expense_date >= start, Expense.expense_date <= end).scalar() or 0

        labels.append(f"{month_abbr[m]} {y}")
        revenue.append(float(ca))
        expenses.append(float(dep))

    return {"labels": labels, "revenue": revenue, "expenses": expenses}


def _build_expense_breakdown(user_id, start, end):
    """Expense breakdown by category over the period."""
    rows = db.session.query(
        ExpenseCategory.name, db.func.coalesce(db.func.sum(Expense.amount), 0)
    ).join(Expense, Expense.category_id == ExpenseCategory.id) \
        .filter(Expense.user_id == user_id,
                Expense.expense_date >= start, Expense.expense_date <= end) \
        .group_by(ExpenseCategory.name) \
        .order_by(db.func.sum(Expense.amount).desc()) \
        .all()

    return {"labels": [r[0] for r in rows], "amounts": [float(r[1]) for r in rows]}


@reports_bp.route("/")
@login_required
@subscription_required
def index():
    today = date.today()
    period = request.args.get("period", "month")
    year = request.args.get("year", today.year, type=int)
    month = request.args.get("month", today.month, type=int)

    try:
        report = _build_report(current_user.id, period, year, month)
        trend = _build_trend(current_user.id, year, month if period == "month" else 12)
        breakdown = _build_expense_breakdown(current_user.id, report["period_start"], report["period_end"])
    except Exception:
        report = None
        trend = None
        breakdown = None

    return render_template(
        "reports/index.html",
        report=report,
        trend=trend,
        breakdown=breakdown,
        period=period,
        year=year,
        month=month,
        today=today,
    )


@reports_bp.route("/export.csv")
@login_required
@subscription_required
def export_csv():
    today = date.today()
    period = request.args.get("period", "month")
    year = request.args.get("year", today.year, type=int)
    month = request.args.get("month", today.month, type=int)

    report = _build_report(current_user.id, period, year, month)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Indicateur", "Valeur"])
    writer.writerow(["Periode debut", report["period_start"].strftime("%d/%m/%Y")])
    writer.writerow(["Periode fin", report["period_end"].strftime("%d/%m/%Y")])
    writer.writerow(["Devis emis", report["quotes_count"]])
    writer.writerow(["Devis acceptes", report["quotes_accepted"]])
    writer.writerow(["Taux de conversion (%)", report["conversion_rate"]])
    writer.writerow(["Factures emises", report["invoices_count"]])
    writer.writerow(["Chiffre d'affaires facture (FCFA)", f"{report['revenue']:.0f}"])
    writer.writerow(["Montant encaisse (FCFA)", f"{report['collected']:.0f}"])
    writer.writerow(["Depenses (FCFA)", f"{report['expenses']:.0f}"])
    writer.writerow(["Solde net (FCFA)", f"{report['net_balance']:.0f}"])

    filename = f"rapport-{report['period_start'].isoformat()}_{report['period_end'].isoformat()}.csv"
    return Response(
        "﻿" + buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
