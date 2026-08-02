"""Email service (Gmail SMTP) — codes de vérification + messages aux contacts."""
from flask import current_app
from flask_mail import Message as MailMessage

from app.extensions import mail

# Charte graphique SenGestion (strictement 3 couleurs)
_MARINE = "#021A3D"
_OR = "#F2B10E"
_JAUNE_PALE = "#E8E7A2"
_LOGO_URL = "https://sengestion.sen-compta.com/static/img/logo.png"


def _branded_html(title: str, name: str, intro: str, code: str, outro: str) -> str:
    """Template HTML des emails transactionnels (code à 6 chiffres).

    Tableau + styles inline : seul markup fiable dans les clients mail
    (Gmail, Outlook…). Couleurs limitées à la charte, or/jaune en fond.
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
            <p style="margin:0 0 8px; font-size:15px; line-height:1.6; color:{_MARINE};">Bonjour <strong>{name}</strong>,</p>
            <p style="margin:0 0 24px; font-size:15px; line-height:1.6; color:{_MARINE};">{intro}</p>
          </td>
        </tr>

        <!-- Code en avant, fond jaune pale -->
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
          <td style="padding:24px 40px 8px; font-family:Arial, Helvetica, sans-serif;">
            <p style="margin:0 0 6px; font-size:14px; line-height:1.6; color:{_MARINE};">
              &#9200;&nbsp;Ce code est valable <strong>15 minutes</strong>.</p>
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


def send_email(to_email: str, subject: str, body: str, html: str | None = None) -> bool:
    """Envoi email générique (message à un contact, relance…).

    `body` (texte brut) sert de repli pour les clients mail sans HTML.
    Retourne True si l'envoi réussit. En développement sans mot de passe SMTP,
    le message est affiché en console et considéré comme envoyé (test offline).
    """
    if not current_app.config.get("MAIL_PASSWORD"):
        current_app.logger.warning(
            "[DEV] SMTP non configuré — email simulé pour %s : %s", to_email, subject
        )
        print(f"\n[DEV] Email à {to_email}\nObjet : {subject}\n{body}\n")
        return True

    try:
        msg = MailMessage(subject=subject, recipients=[to_email], body=body, html=html)
        mail.send(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("Échec envoi email à %s : %s", to_email, exc)
        return False


def send_verification_code(to_email: str, name: str, code: str) -> bool:
    """Send the 6-digit verification code by email.

    Returns True on success. In development, if no SMTP password is set,
    the code is printed to the console so the flow can be tested offline.
    """
    subject = "SenGestion — Code de vérification"
    body = (
        f"Bonjour {name},\n\n"
        f"Merci de votre inscription sur SenGestion.\n"
        f"Votre code de vérification est : {code}\n\n"
        f"Ce code est valable 15 minutes.\n"
        f"Si vous n'êtes pas à l'origine de cette inscription, ignorez cet email.\n\n"
        f"— L'équipe SenGestion"
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

    # Développement : pas de mot de passe SMTP -> on affiche le code en console
    if not current_app.config.get("MAIL_PASSWORD"):
        current_app.logger.warning(
            "[DEV] SMTP non configuré — code de vérification pour %s : %s", to_email, code
        )
        print(f"\n[DEV] Code de vérification pour {to_email} : {code}\n")
        return True

    try:
        msg = MailMessage(subject=subject, recipients=[to_email], body=body, html=html)
        mail.send(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("Échec envoi email à %s : %s", to_email, exc)
        return False


def send_password_reset_code(to_email: str, name: str, code: str) -> bool:
    """Envoie le code à 6 chiffres de réinitialisation du mot de passe."""
    subject = "SenGestion — Réinitialisation du mot de passe"
    body = (
        f"Bonjour {name},\n\n"
        f"Vous avez demandé à réinitialiser votre mot de passe SenGestion.\n"
        f"Votre code de confirmation est : {code}\n\n"
        f"Ce code est valable 15 minutes.\n"
        f"Si vous n'êtes pas à l'origine de cette demande, ignorez cet email : "
        f"votre mot de passe reste inchangé.\n\n"
        f"— L'équipe SenGestion"
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
