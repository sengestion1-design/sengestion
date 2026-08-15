"""Email service (Gmail SMTP) - verification codes + messages to contacts."""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app
from flask_mail import Message as MailMessage
from markupsafe import escape

from app.extensions import mail
from app.services.crypto_service import decrypt

# SenGestion brand palette (strictly 3 colors)
_MARINE = "#021A3D"
_OR = "#F2B10E"
_JAUNE_PALE = "#E8E7A2"
_LOGO_URL = "https://sengestion.sen-compta.com/static/img/logo.png"


def _branded_html_shell(title: str, name: str, intro: str, body_html: str, outro: str) -> str:
    """Base HTML shell for transactional emails (header/footer/brand palette).

    Table + inline styles: the only reliable markup in mail clients
    (Gmail, Outlook…). Colors limited to the brand palette, gold/yellow as background.
    `body_html` is the free-form content block placed between intro and outro.
    """
    return f"""\
<!doctype html>
<html lang="fr">
<body style="margin:0; padding:0; background-color:#f4f5f7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f5f7; padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0"
             style="max-width:560px; width:100%; background-color:#ffffff; border-radius:14px; overflow:hidden; border:1px solid #e2e6ea;">

        <!-- En-tete marine avec logo -->
        <tr>
          <td align="center" style="background-color:{_MARINE}; padding:28px 24px 24px;">
            <img src="{_LOGO_URL}" alt="SenGestion" width="150"
                 style="display:block; width:150px; height:auto; border:0;">
          </td>
        </tr>
        <!-- Filet or -->
        <tr><td style="background-color:{_OR}; height:5px; font-size:0; line-height:0;">&nbsp;</td></tr>

        <!-- Corps -->
        <tr>
          <td style="padding:36px 40px 12px; font-family:Arial, Helvetica, sans-serif;">
            <h1 style="margin:0 0 18px; font-family:'Palatino Linotype','Book Antiqua',Palatino,Georgia,serif;
                       font-size:24px; line-height:1.3; color:{_MARINE};">{title}</h1>
            <p style="margin:0 0 8px; font-size:15px; line-height:1.6; color:{_MARINE};">Bonjour <strong>{escape(name)}</strong>,</p>
            <p style="margin:0 0 24px; font-size:15px; line-height:1.6; color:{_MARINE};">{intro}</p>
          </td>
        </tr>

        {body_html}

        <tr>
          <td style="padding:24px 40px 8px; font-family:Arial, Helvetica, sans-serif;">
            <p style="margin:0 0 24px; font-size:13px; line-height:1.6; color:#5a6b80;">{outro}</p>
          </td>
        </tr>

        <!-- Pied de page -->
        <tr>
          <td align="center" style="background-color:{_MARINE}; padding:20px 24px;">
            <p style="margin:0 0 4px; font-family:Arial, Helvetica, sans-serif; font-size:13px; color:{_JAUNE_PALE};">
              L'&eacute;quipe SenGestion</p>
            <p style="margin:0; font-family:Arial, Helvetica, sans-serif; font-size:12px; color:{_JAUNE_PALE};">
              <a href="https://sengestion.sen-compta.com" style="color:{_JAUNE_PALE}; text-decoration:underline;">sengestion.sen-compta.com</a>
              &nbsp;&middot;&nbsp; La gestion simple pour les PME s&eacute;n&eacute;galaises</p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _branded_html(title: str, name: str, intro: str, code: str, outro: str) -> str:
    """HTML template for transactional emails showing a 6-digit code."""
    code_block = f"""\
        <tr>
          <td align="center" style="padding:0 40px;">
            <table role="presentation" cellpadding="0" cellspacing="0" width="100%">
              <tr>
                <td align="center" style="background-color:{_JAUNE_PALE}; border-radius:12px; padding:22px 16px;">
                  <p style="margin:0 0 6px; font-family:Arial, Helvetica, sans-serif; font-size:12px;
                            letter-spacing:2px; text-transform:uppercase; color:{_MARINE};">Votre code de confirmation</p>
                  <p style="margin:0; font-family:Arial, Helvetica, sans-serif; font-size:36px; font-weight:bold;
                            letter-spacing:10px; color:{_MARINE};">{code}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:18px 40px 0; font-family:Arial, Helvetica, sans-serif;">
            <p style="margin:0; font-size:14px; line-height:1.6; color:{_MARINE};">
              &#9200;&nbsp;Ce code est valable <strong>15 minutes</strong>.</p>
          </td>
        </tr>"""
    return _branded_html_shell(title, name, intro, code_block, outro)


def send_email(to_email: str, subject: str, body: str, html: str | None = None) -> bool:
    """Generic email send (message to a contact, reminder…).

    `body` (plain text) serves as a fallback for mail clients without HTML.
    Returns True if the send succeeds. In development without an SMTP password,
    the message is printed to the console and considered sent (offline testing).
    """
    if not current_app.config.get("MAIL_PASSWORD"):
        current_app.logger.warning(
            "[DEV] SMTP not configured - simulated email for %s: %s", to_email, subject
        )
        print(f"\n[DEV] Email à {to_email}\nObjet : {subject}\n{body}\n")
        return True

    try:
        msg = MailMessage(subject=subject, recipients=[to_email], body=body, html=html)
        mail.send(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


def send_email_as_manager(settings, sender_display_name: str, to_email: str,
                           subject: str, body: str) -> bool:
    """Send a message to a contact/client FROM the manager's own mailbox.

    Used only for manager -> contact messages (messages.send), never for
    transactional emails (verification code, password reset, admin
    notifications), which always go through the shared SenGestion mailbox
    via send_email(). Falls back to send_email() if no custom SMTP is
    configured, or if the stored password fails to decrypt.
    """
    if not settings or not settings.has_custom_smtp:
        return send_email(to_email, subject, body)

    password = decrypt(settings.smtp_password_encrypted)
    if not password:
        current_app.logger.warning(
            "SMTP password could not be decrypted for manager settings id=%s", settings.id
        )
        return send_email(to_email, subject, body)

    from_header = f"{sender_display_name} <{settings.smtp_email}>" if sender_display_name else settings.smtp_email

    msg = MIMEMultipart()
    msg["From"] = from_header
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        port = settings.smtp_port or 587
        with smtplib.SMTP(settings.smtp_host, port, timeout=15) as server:
            if settings.smtp_use_tls:
                server.starttls()
            server.login(settings.smtp_email, password)
            server.sendmail(settings.smtp_email, [to_email], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error(
            "Failed to send email via manager SMTP (%s) to %s: %s",
            settings.smtp_email, to_email, exc,
        )
        return False


def send_verification_code(to_email: str, name: str, code: str) -> bool:
    """Send the 6-digit verification code by email.

    Returns True on success. In development, if no SMTP password is set,
    the code is printed to the console so the flow can be tested offline.
    """
    subject = "SenGestion - Code de vérification"
    body = (
        f"Bonjour {name},\n\n"
        f"Merci de votre inscription sur SenGestion.\n"
        f"Votre code de vérification est : {code}\n\n"
        f"Ce code est valable 15 minutes.\n"
        f"Si vous n'êtes pas à l'origine de cette inscription, ignorez cet email.\n\n"
        f"- L'équipe SenGestion"
    )

    html = _branded_html(
        title="Bienvenue sur SenGestion !",
        name=name,
        intro="Merci de votre inscription. Pour activer votre compte, "
              "saisissez le code ci-dessous sur la page de v&eacute;rification.",
        code=code,
        outro="Si vous n'&ecirc;tes pas &agrave; l'origine de cette inscription, "
              "ignorez simplement cet email.",
    )

    # Development: no SMTP password -> print the code to the console
    if not current_app.config.get("MAIL_PASSWORD"):
        current_app.logger.warning(
            "[DEV] SMTP not configured - verification code for %s: %s", to_email, code
        )
        print(f"\n[DEV] Code de vérification pour {to_email} : {code}\n")
        return True

    try:
        msg = MailMessage(subject=subject, recipients=[to_email], body=body, html=html)
        mail.send(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


def send_password_reset_code(to_email: str, name: str, code: str) -> bool:
    """Send the 6-digit password reset code."""
    subject = "SenGestion - Réinitialisation du mot de passe"
    body = (
        f"Bonjour {name},\n\n"
        f"Vous avez demandé à réinitialiser votre mot de passe SenGestion.\n"
        f"Votre code de confirmation est : {code}\n\n"
        f"Ce code est valable 15 minutes.\n"
        f"Si vous n'êtes pas à l'origine de cette demande, ignorez cet email : "
        f"votre mot de passe reste inchangé.\n\n"
        f"- L'équipe SenGestion"
    )
    html = _branded_html(
        title="R&eacute;initialisation de votre mot de passe",
        name=name,
        intro="Vous avez demand&eacute; &agrave; r&eacute;initialiser le mot de passe "
              "de votre compte SenGestion. Saisissez le code ci-dessous sur la page "
              "de r&eacute;initialisation pour choisir un nouveau mot de passe.",
        code=code,
        outro="Si vous n'&ecirc;tes pas &agrave; l'origine de cette demande, ignorez "
              "cet email&nbsp;: votre mot de passe reste inchang&eacute;.",
    )
    return send_email(to_email, subject, body, html=html)


_PLAN_LABELS = {"monthly": "Mensuel", "annual": "Annuel"}
_METHOD_LABELS = {"orange_money": "Orange Money", "wave": "Wave"}


def send_payment_proof_notification(
    admin_email: str, admin_name: str, manager_name: str, manager_company: str,
    plan: str, method: str, transaction_number: str, review_url: str,
) -> bool:
    """Notify the admin that a manager submitted a subscription payment proof to review."""
    subject = f"SenGestion - Preuve de paiement à valider ({manager_name})"
    plan_label = _PLAN_LABELS.get(plan, plan)
    method_label = _METHOD_LABELS.get(method, method)

    # Valeurs saisies par l'utilisateur : à échapper avant interpolation HTML
    safe_manager = escape(manager_name)
    safe_company = escape(manager_company or "sans entreprise")
    safe_transaction = escape(transaction_number)
    safe_plan = escape(plan_label)
    safe_method = escape(method_label)

    body = (
        f"Bonjour {admin_name},\n\n"
        f"{manager_name} ({manager_company or 'sans entreprise'}) a soumis une preuve de "
        f"paiement pour son abonnement.\n\n"
        f"Plan : {plan_label}\n"
        f"Moyen de paiement : {method_label}\n"
        f"Numéro de transaction : {transaction_number}\n\n"
        f"Consultez et validez la demande depuis votre interface d'administration :\n"
        f"{review_url}\n\n"
        f"- L'équipe SenGestion"
    )

    body_block = f"""\
        <tr>
          <td style="padding:0 40px 24px; font-family:Arial, Helvetica, sans-serif;">
            <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
                   style="background-color:{_JAUNE_PALE}; border-radius:12px;">
              <tr>
                <td style="padding:20px 22px; font-size:14px; line-height:1.8; color:{_MARINE};">
                  <strong>Gérant :</strong> {safe_manager} ({safe_company})<br>
                  <strong>Plan :</strong> {safe_plan}<br>
                  <strong>Moyen de paiement :</strong> {safe_method}<br>
                  <strong>N&deg; de transaction :</strong> {safe_transaction}
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td align="center" style="padding:0 40px 8px;">
            <a href="{review_url}"
               style="display:inline-block; background-color:{_OR}; color:{_MARINE}; font-weight:bold;
                      font-family:Arial, Helvetica, sans-serif; font-size:15px; text-decoration:none;
                      padding:14px 28px; border-radius:8px;">Examiner la demande</a>
          </td>
        </tr>"""

    html = _branded_html_shell(
        title="Preuve de paiement re&ccedil;ue",
        name=admin_name,
        intro=f"<strong>{safe_manager}</strong> a soumis une preuve de paiement pour "
              f"son abonnement SenGestion. Voici le d&eacute;tail de la demande :",
        body_html=body_block,
        outro="Une fois v&eacute;rifi&eacute;e, validez ou rejetez la demande depuis "
              "l'interface d'administration.",
    )
    return send_email(admin_email, subject, body, html=html)
