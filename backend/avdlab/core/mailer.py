"""Outgoing e-mail via SMTP (v0.9.0, stream A).

    ok = await send_mail(subject, text, to=None, html=None)   # never raises; False when SMTP is not configured
    ok, error = await send_mail_result(...)                     # same, with the error text (admin "test mail")
    spawn(coro)                                                 # fire-and-forget, exceptions are logged

Configuration comes from the admin settings (`core/app_settings.py`: smtp_host/port/user/starttls/from, password in
the secret store). Port 465 → implicit TLS; otherwise STARTTLS when `smtp_starttls` is on. `to` defaults to
`feedback_mail_to` and may be a comma-separated list. The SMTP password never appears in a log line or error.
"""
from __future__ import annotations

import asyncio
import logging
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Coroutine, Iterable

import aiosmtplib

from . import app_settings

log = logging.getLogger("avdlab.mailer")
TIMEOUT = 20.0
_tasks: set[asyncio.Task] = set()


def _scrub(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "****")
    return text


def build_message(subject: str, text: str, sender: str, to: list[str], html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:250]
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] or "localhost")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


async def send_mail_result(subject: str, text: str, to: str | Iterable[str] | None = None,
                           html: str | None = None) -> tuple[bool, str | None]:
    """Send one message; returns (ok, error). Never raises."""
    password = ""
    try:
        cfg = await app_settings.load()
        if not cfg.smtp_configured:
            return False, "SMTP is not configured (smtp_host is empty)"
        rcpts = app_settings.split_addresses(to if isinstance(to, str) else ", ".join(to)) if to else \
            app_settings.split_addresses(cfg.feedback_mail_to)
        if not rcpts:
            return False, "no recipient"
        password = await app_settings.smtp_password()
        port = int(cfg.smtp_port or 587)
        implicit = port == 465
        smtp = aiosmtplib.SMTP(hostname=cfg.smtp_host.strip(), port=port, use_tls=implicit,
                               start_tls=(bool(cfg.smtp_starttls) and not implicit), timeout=TIMEOUT)
        msg = build_message(subject, text, cfg.smtp_from, rcpts, html)
        await smtp.connect()
        try:
            if cfg.smtp_user:
                await smtp.login(cfg.smtp_user, password)
            await smtp.send_message(msg)
        finally:
            try:
                await smtp.quit()
            except Exception:  # noqa: BLE001 - the message is already sent (or the error is reported below)
                pass
        log.info("mail sent: %r → %s", msg["Subject"], ", ".join(rcpts))
        return True, None
    except Exception as exc:  # noqa: BLE001 - mail is best effort, callers only get a bool
        err = _scrub(f"{type(exc).__name__}: {exc}", password)
        log.warning("mail %r not sent: %s", subject, err)
        return False, err


async def send_mail(subject: str, text: str, to: str | Iterable[str] | None = None, html: str | None = None) -> bool:
    ok, _ = await send_mail_result(subject, text, to=to, html=html)
    return ok


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Run `coro` in the background (keeps a reference so it is not garbage-collected; logs failures)."""
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("background mail task failed: %s", t.exception())

    task.add_done_callback(_done)
    return task


async def drain() -> None:
    """Wait for all background mail tasks (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
