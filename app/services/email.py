"""
Serviço de e-mail transacional via Resend.
"""

import resend

from app.config import settings


def _configure() -> None:
    resend.api_key = settings.RESEND_API_KEY


def _base_layout(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#0D1117;font-family:system-ui,-apple-system,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0D1117;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#161B22;border-radius:16px;overflow:hidden;border:1px solid #30363D;">
          <tr>
            <td style="background:#3FB950;padding:22px 32px;text-align:center;">
              <h1 style="margin:0;color:#020B09;font-size:21px;font-weight:800;letter-spacing:-0.5px;">
                🌱 Let's Grow
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              {body_html}
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 24px;border-top:1px solid #30363D;">
              <p style="margin:0;color:#484F58;font-size:12px;text-align:center;">
                Let's Grow — cultivando com tecnologia
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _otp_box(code: str) -> str:
    return f"""
<table width="100%" cellpadding="0" cellspacing="0">
  <tr>
    <td align="center" style="padding:24px 0;">
      <div style="display:inline-block;background:#0D1117;border:2px solid #3FB950;
                  border-radius:12px;padding:20px 40px;">
        <span style="font-size:40px;font-weight:800;letter-spacing:12px;color:#3FB950;
                     font-variant-numeric:tabular-nums;">
          {code}
        </span>
      </div>
    </td>
  </tr>
</table>"""


async def send_verification_email(to_email: str, name: str, code: str) -> None:
    """Envia o codigo OTP de verificacao de email no cadastro."""
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY nao configurada")
    _configure()
    display = name or to_email.split("@")[0]
    body = f"""
<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Confirme seu e-mail</h2>
<p style="margin:0 0 8px;color:#8B949E;font-size:15px;line-height:1.6;">
  Ola, <strong style="color:#E6EDF3;">{display}</strong>! Bem-vindo ao Let's Grow.
</p>
<p style="margin:0 0 24px;color:#8B949E;font-size:15px;line-height:1.6;">
  Use o codigo abaixo para confirmar seu e-mail.
  Ele e valido por <strong style="color:#E6EDF3;">15 minutos</strong>.
</p>
{_otp_box(code)}
<p style="margin:0;color:#8B949E;font-size:13px;line-height:1.5;">
  Se voce nao criou uma conta no Let's Grow, ignore este e-mail.
</p>"""
    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": f"{code} — Confirme seu e-mail no Let's Grow",
        "html": _base_layout("Confirme seu e-mail", body),
    })


async def send_welcome_email(to_email: str, name: str) -> None:
    """Envia e-mail de boas-vindas apos verificacao bem-sucedida."""
    if not settings.RESEND_API_KEY:
        return  # silencioso — boas-vindas nao e critico
    _configure()
    display = name or to_email.split("@")[0]
    body = f"""
<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Bem-vindo ao Let's Grow! 🌱</h2>
<p style="margin:0 0 16px;color:#8B949E;font-size:15px;line-height:1.6;">
  Ola, <strong style="color:#E6EDF3;">{display}</strong>!
</p>
<p style="margin:0 0 16px;color:#8B949E;font-size:15px;line-height:1.6;">
  Sua conta foi confirmada com sucesso. Voce ja pode acessar o app e comecar
  a registrar seu cultivo, acompanhar suas plantas e conversar com o Bob.
</p>
<p style="margin:0;color:#8B949E;font-size:13px;line-height:1.5;">
  Qualquer duvida, e so responder este e-mail. Bom cultivo!
</p>"""
    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": "Bem-vindo ao Let's Grow!",
        "html": _base_layout("Bem-vindo ao Let's Grow!", body),
    })


async def send_password_reset_email(to_email: str, code: str) -> None:
    """Envia o codigo OTP de redefinicao de senha."""
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY nao configurada")
    _configure()
    body = f"""
<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Redefinicao de senha</h2>
<p style="margin:0 0 24px;color:#8B949E;font-size:15px;line-height:1.6;">
  Recebemos uma solicitacao para redefinir a senha da sua conta.
  Use o codigo abaixo — ele e valido por <strong style="color:#E6EDF3;">15 minutos</strong>.
</p>
{_otp_box(code)}
<p style="margin:0;color:#8B949E;font-size:13px;line-height:1.5;">
  Se voce nao solicitou a redefinicao, ignore este e-mail.
  Sua senha <strong style="color:#E6EDF3;">nao sera alterada</strong>.
</p>"""
    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": f"{code} — Seu codigo de redefinicao de senha (Let's Grow)",
        "html": _base_layout("Redefinicao de senha", body),
    })
