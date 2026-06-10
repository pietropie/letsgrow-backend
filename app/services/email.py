"""
Servico de e-mail transacional via Resend.

Uso:
    await send_password_reset_email(to_email="user@example.com", code="123456")
"""

import resend

from app.config import settings


def _client() -> None:
    """Configura a chave global do Resend (chamado lazy)."""
    resend.api_key = settings.RESEND_API_KEY


async def send_password_reset_email(to_email: str, code: str) -> None:
    """Envia o e-mail com o código OTP de redefinição de senha."""
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY não configurada")

    _client()

    html = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Redefinição de senha — Let's Grow</title>
</head>
<body style="margin:0;padding:0;background:#0D1117;font-family:system-ui,-apple-system,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0D1117;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0"
               style="background:#161B22;border-radius:16px;overflow:hidden;border:1px solid #30363D;">
          <!-- Header -->
          <tr>
            <td style="background:#3FB950;padding:24px 32px;text-align:center;">
              <h1 style="margin:0;color:#020B09;font-size:22px;font-weight:800;letter-spacing:-0.5px;">
                🌱 Let's Grow
              </h1>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding:32px;">
              <h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">
                Redefinição de senha
              </h2>
              <p style="margin:0 0 24px;color:#8B949E;font-size:15px;line-height:1.6;">
                Recebemos uma solicitação para redefinir a senha da sua conta.
                Use o código abaixo — ele é válido por <strong style="color:#E6EDF3;">15 minutos</strong>.
              </p>

              <!-- OTP box -->
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
              </table>

              <p style="margin:0 0 8px;color:#8B949E;font-size:13px;line-height:1.5;">
                Se você não solicitou a redefinição de senha, ignore este e-mail.
                Sua senha <strong style="color:#E6EDF3;">não será alterada</strong>.
              </p>
            </td>
          </tr>
          <!-- Footer -->
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
</html>
"""

    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": f"{code} — Seu codigo de redefinicao de senha (Let's Grow)",
        "html": html,
    })
