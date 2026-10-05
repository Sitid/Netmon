"""Notificaciones por mail (opcional). Si NETMON_SMTP_HOST está vacío, no hace nada."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from .config import get_settings

log = logging.getLogger("netmon.notify")


def _send_sync(subject: str, body: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = f"[netmon] {subject}"
    msg["From"] = s.smtp_from
    msg["To"] = s.smtp_to
    msg.set_content(body)

    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        if s.smtp_tls:
            smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_pass)
        smtp.send_message(msg)


async def send_mail(subject: str, body: str) -> None:
    """Envía mail en un thread para no bloquear el event loop. Nunca levanta excepción."""
    s = get_settings()
    if not s.smtp_host or not s.smtp_to:
        return
    try:
        await asyncio.to_thread(_send_sync, subject, body)
        log.info("Mail enviado: %s", subject)
    except Exception:
        log.exception("Fallo al enviar mail (la alerta ya quedó registrada en DB)")


async def send_webhook(payload: dict) -> None:
    """POST JSON al webhook configurado (formato genérico, apto para un relay
    hacia Telegram/Teams). Nunca levanta excepción."""
    s = get_settings()
    if not s.webhook_url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(s.webhook_url, json=payload)
        log.info("Webhook enviado: %s", payload.get("kind"))
    except Exception:
        log.exception("Fallo al enviar webhook (la alerta ya quedó registrada en DB)")
