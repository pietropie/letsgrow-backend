"""
Serviço de e-mail transacional via Resend.
"""

import html

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


# ── Conta e suporte ───────────────────────────────────────────────────────────

def _send(to: list[str], subject: str, title: str, body: str, reply_to: str | None = None) -> None:
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY nao configurada")
    _configure()
    payload = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": to,
        "subject": subject,
        "html": _base_layout(title, body),
    }
    if reply_to:
        payload["reply_to"] = reply_to
    resend.Emails.send(payload)


def _p(text: str, *, small: bool = False) -> str:
    size = "13px" if small else "15px"
    return f'<p style="margin:0 0 16px;color:#8B949E;font-size:{size};line-height:1.6;">{text}</p>'


def _date_br(value) -> str:
    from zoneinfo import ZoneInfo
    return value.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y")


async def send_support_to_inbox(
    inbox: str, *, message_id: str, user_email: str, subject: str, message: str,
) -> None:
    """Encaminha a mensagem do usuario para a caixa de suporte (responder vai ao usuario)."""
    safe_subject = html.escape(subject)
    body = (
        f'<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">{safe_subject}</h2>'
        + _p(f"De: {html.escape(user_email)} · protocolo {html.escape(message_id)}", small=True)
        + _p(html.escape(message).replace("\n", "<br>"))
    )
    _send([inbox], f"[Suporte] {subject}", "Mensagem de suporte", body, reply_to=user_email)


async def send_support_receipt(to_email: str, *, subject: str) -> None:
    body = (
        '<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Mensagem recebida</h2>'
        + _p(f"Recebemos sua mensagem sobre <strong style=\"color:#E6EDF3;\">{html.escape(subject)}</strong>.")
        + _p("Você receberá o retorno por este e-mail. O envio ao suporte não consome créditos.")
    )
    _send([to_email], "Recebemos sua mensagem — Let's Grow", "Mensagem recebida", body)


async def send_export_ready(to_email: str, *, expires_at) -> None:
    body = (
        '<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Sua cópia dos dados está pronta</h2>'
        + _p("Abra o Let's Grow e vá em <strong style=\"color:#E6EDF3;\">Conta › Privacidade e dados › "
             "Cópia dos seus dados</strong> para baixar o arquivo.")
        + _p(f"Por segurança, o arquivo fica disponível até {_date_br(expires_at)} e só pode ser baixado "
             "com a sua conta conectada. Este e-mail não contém link de download.", small=True)
    )
    _send([to_email], "Sua cópia dos dados está pronta — Let's Grow", "Cópia dos dados", body)


async def send_deletion_scheduled(to_email: str, *, scheduled_for) -> None:
    body = (
        '<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Solicitação de exclusão recebida</h2>'
        + _p(f"Sua conta e o conteúdo associado serão excluídos em {_date_br(scheduled_for)}.")
        + _p("Até essa data você ainda pode entrar no app e cancelar a exclusão em "
             "<strong style=\"color:#E6EDF3;\">Conta › Privacidade e dados › Excluir minha conta</strong>.")
        + _p("Se você não fez este pedido, entre no app e cancele, e troque sua senha.", small=True)
    )
    _send([to_email], "Solicitação de exclusão de conta — Let's Grow", "Exclusão de conta", body)


async def send_deletion_completed(to_email: str) -> None:
    body = (
        '<h2 style="margin:0 0 12px;color:#E6EDF3;font-size:20px;">Conta excluída</h2>'
        + _p("Sua conta, ambientes, plantas, registros, fotos e a memória do Bobby foram excluídos.")
        + _p("Registros de compra, se existirem, são mantidos sem vínculo com você pelo prazo exigido "
             "pela legislação fiscal.", small=True)
    )
    _send([to_email], "Sua conta foi excluída — Let's Grow", "Conta excluída", body)
