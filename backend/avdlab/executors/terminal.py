"""Web terminal: WebSocket ⇄ PTY process over SSH on the lab host.

  node, shell=cli   docker exec -it <node> Cli
  node, shell=bash  docker exec -it <node> bash      (linux clients: bash if present, else sh — for both shells)
  host              $SHELL -l
  ACT lab node      ssh <act user>@<mgmt ip> from the tools server (+ `bash` for shell=bash)

Frames: binary both ways (client → stdin, stdout → client); a JSON text frame `{"resize": {"cols", "rows"}}` resizes
the PTY; any other text frame is written to stdin as UTF-8. Session open/close is logged (never the content).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from typing import Any, Protocol

log = logging.getLogger("avdlab.terminal")

TERM_TYPE = "xterm-256color"
READ_CHUNK = 16384
CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4400
CLOSE_NOT_FOUND = 4404
CLOSE_CONFLICT = 4409
CLOSE_ERROR = 1011


class PtyProcess(Protocol):
    stdin: Any
    stdout: Any

    def change_terminal_size(self, width: int, height: int, pixwidth: int = 0, pixheight: int = 0) -> None: ...

    def close(self) -> None: ...


def clamp_size(cols: Any, rows: Any) -> tuple[int, int]:
    def num(v: Any, default: int, hi: int) -> int:
        try:
            return max(1, min(int(v), hi))
        except (TypeError, ValueError):
            return default

    return num(cols, 80, 1000), num(rows, 24, 500)


def node_command(node: str, shell: str, kind: str) -> str:
    q = shlex.quote(node)
    if kind == "linux":
        return f"docker exec -it {q} sh -c " + shlex.quote("command -v bash >/dev/null 2>&1 && exec bash -l || exec sh -l")
    return f"docker exec -it {q} Cli" if shell == "cli" else f"docker exec -it {q} bash"


HOST_COMMAND = "$SHELL -l"


def act_node_command(user: str, ip: str, shell: str) -> str:
    """ACT labs: interactive ssh from the tools server to the node's mgmt IP (password typed in the terminal)."""
    cmd = (f"ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "
           f"{shlex.quote(user)}@{shlex.quote(ip)}")
    return cmd + " bash" if shell == "bash" else cmd


def handle_text(proc: PtyProcess, text: str) -> None:
    """Resize frames → PTY size; anything else is keyboard input."""
    try:
        msg = json.loads(text)
    except ValueError:
        msg = None
    if isinstance(msg, dict) and isinstance(msg.get("resize"), dict):
        cols, rows = clamp_size(msg["resize"].get("cols"), msg["resize"].get("rows"))
        proc.change_terminal_size(cols, rows)
        return
    proc.stdin.write(text.encode())


async def bridge(ws: Any, proc: PtyProcess, *, label: str = "") -> None:
    """Pump until either side ends; always closes the process. `ws` needs `receive()` (ASGI messages) and
    `send_bytes()` (starlette WebSocket)."""
    t0 = time.monotonic()
    log.info("terminal open %s", label)

    async def ws_to_proc() -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                proc.stdin.write(msg["bytes"])
            elif msg.get("text") is not None:
                handle_text(proc, msg["text"])

    async def proc_to_ws() -> None:
        while True:
            data = await proc.stdout.read(READ_CHUNK)
            if not data:
                return
            await ws.send_bytes(data if isinstance(data, bytes) else str(data).encode())

    tasks = [asyncio.create_task(ws_to_proc()), asyncio.create_task(proc_to_ws())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc is not None and not _is_disconnect(exc):
                log.warning("terminal %s ended with %r", label, exc)
    finally:
        for t in tasks:
            t.cancel()
        try:
            proc.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("terminal close %s after %.1fs", label, time.monotonic() - t0)


def _is_disconnect(exc: BaseException) -> bool:
    return type(exc).__name__ in ("WebSocketDisconnect", "ClientDisconnected", "ConnectionClosed")


async def serve(ws: Any, host: Any, command: str | None, *, cols: int, rows: int, label: str) -> None:
    """Accepted WebSocket → SSH to `host` → PTY `command` → bridge. Errors go to the terminal, then close 1011."""
    from ..core.db import SessionLocal
    from ..hosts.ssh import SSHClient

    try:
        async with SessionLocal() as s:
            async with SSHClient.connect(host, s) as ssh:
                proc = await ssh.open_pty(command, cols=cols, rows=rows, term_type=TERM_TYPE)
                await bridge(ws, proc, label=label)
    except Exception as exc:  # noqa: BLE001 - report to the user, keep the server alive
        if _is_disconnect(exc):
            return
        log.warning("terminal %s failed: %s", label, exc)
        try:
            await ws.send_bytes(f"\r\n[avdlab] terminal error: {exc}\r\n".encode())
            await ws.close(code=CLOSE_ERROR)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
