"""Web terminal: WebSocket ⇄ PTY bridge (fake PTY process), commands, auth and the two WS endpoints."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from avdlab.core.db import LabState
from avdlab.executors import terminal
from avdlab.hosts.ssh import SSHClient

from .test_executors import _cleanup, mk_host, mk_lab  # noqa: F401


class FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data


class FakeStdout:
    def __init__(self) -> None:
        self.q: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, n: int) -> bytes:
        return await self.q.get()


class FakePty:
    """Echoes stdin to stdout like a terminal; `exit` ends the process (EOF)."""

    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.sizes: list[tuple[int, int]] = []
        self.closed = False
        orig = self.stdin.write

        def write(data: bytes) -> None:
            orig(data)
            if data == b"exit\r":
                self.stdout.q.put_nowait(b"")
            else:
                self.stdout.q.put_nowait(b"echo:" + data)

        self.stdin.write = write  # type: ignore[method-assign]

    def change_terminal_size(self, width: int, height: int, pixwidth: int = 0, pixheight: int = 0) -> None:
        self.sizes.append((width, height))

    def close(self) -> None:
        self.closed = True


class FakeWS:
    def __init__(self, messages: list[dict[str, Any]], *, hold: bool = False) -> None:
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for m in messages:
            self.inbox.put_nowait(m)
        self.hold = hold
        self.sent: list[bytes] = []

    async def receive(self) -> dict[str, Any]:
        if self.inbox.empty() and not self.hold:
            await asyncio.sleep(0.05)
            return {"type": "websocket.disconnect", "code": 1000}
        return await self.inbox.get()

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


def rx(data: bytes | None = None, text: str | None = None) -> dict[str, Any]:
    return {"type": "websocket.receive", "bytes": data, "text": text}


async def test_bridge_pumps_both_ways_and_resizes():
    proc = FakePty()
    ws = FakeWS([rx(b"show version\r"), rx(text=json.dumps({"resize": {"cols": 132, "rows": 40}})),
                 rx(text="ls\r"), rx(text=json.dumps({"resize": {"cols": 99999, "rows": "x"}})), rx(b"exit\r")], hold=True)
    await asyncio.wait_for(terminal.bridge(ws, proc, label="test"), 2)
    assert proc.stdin.data == b"show version\rls\rexit\r"
    assert proc.sizes == [(132, 40), (1000, 24)]
    assert ws.sent == [b"echo:show version\r", b"echo:ls\r"]
    assert proc.closed  # process EOF ends the session and the process is closed


async def test_bridge_closes_process_on_disconnect():
    proc = FakePty()
    ws = FakeWS([rx(b"a")])  # then the browser goes away
    await asyncio.wait_for(terminal.bridge(ws, proc), 2)
    assert proc.closed and ws.sent == [b"echo:a"]


def test_commands_and_sizes():
    assert terminal.node_command("dc1-spine1", "cli", "arista_ceos") == "docker exec -it dc1-spine1 Cli"
    assert terminal.node_command("dc1-spine1", "bash", "arista_ceos") == "docker exec -it dc1-spine1 bash"
    assert terminal.node_command("srv1", "cli", "linux").startswith("docker exec -it srv1 sh -c ")
    assert terminal.HOST_COMMAND == "$SHELL -l"
    assert terminal.clamp_size("120", None) == (120, 24) and terminal.clamp_size(0, 5000) == (1, 500)


# ------------------------------------------------------------------------------------------------ endpoints
class TermSSH:
    def __init__(self) -> None:
        self.opened: list[tuple[str | None, int, int, str]] = []
        self.proc = FakePty()

    async def open_pty(self, cmd, *, cols=80, rows=24, term_type="xterm-256color"):  # noqa: ANN001
        self.opened.append((cmd, cols, rows, term_type))
        return self.proc


@pytest.fixture
def term_ssh(monkeypatch):
    fake = TermSSH()

    @asynccontextmanager
    async def fake_connect(cls, host, session, **kw):  # noqa: ANN001
        yield fake

    monkeypatch.setattr(SSHClient, "connect", classmethod(fake_connect))
    return fake


def _login(tc: TestClient) -> None:
    assert tc.post("/api/auth/login", json={"username": "admin", "password": "test"}).status_code == 200


async def test_terminal_endpoints(client, term_ssh):
    from avdlab.main import app

    host_id = await mk_host()
    lab = await mk_lab(client, host_id=host_id, state=LabState.deployed)
    built = await mk_lab(client, host_id=host_id, state=LabState.built)

    def scenario() -> dict[str, Any]:
        out: dict[str, Any] = {}
        tc = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc:  # no session cookie
            with tc.websocket_connect(f"/api/labs/{lab['id']}/nodes/dc1-spine1/terminal") as ws:
                ws.receive_bytes()
        out["unauth"] = exc.value.code
        with pytest.raises(WebSocketDisconnect) as exc:
            with tc.websocket_connect(f"/api/hosts/{host_id}/terminal") as ws:
                ws.receive_bytes()
        out["unauth_host"] = exc.value.code
        _login(tc)
        with tc.websocket_connect(f"/api/labs/{lab['id']}/nodes/dc1-spine1/terminal?shell=bash&cols=100&rows=30") as ws:
            ws.send_bytes(b"uptime\r")
            out["echo"] = ws.receive_bytes()
            ws.send_text(json.dumps({"resize": {"cols": 90, "rows": 20}}))
            ws.send_bytes(b"exit\r")
            with pytest.raises(WebSocketDisconnect):
                ws.receive_bytes()
        for key, url in (("ghost", f"/api/labs/{lab['id']}/nodes/ghost/terminal"),
                         ("not_deployed", f"/api/labs/{built['id']}/nodes/dc1-spine1/terminal"),
                         ("bad_shell", f"/api/labs/{lab['id']}/nodes/dc1-spine1/terminal?shell=zsh")):
            with tc.websocket_connect(url) as ws:
                out[key + "_msg"] = ws.receive_bytes()
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_bytes()
                out[key] = exc.value.code
        term_ssh.proc = FakePty()
        with tc.websocket_connect(f"/api/hosts/{host_id}/terminal?cols=80&rows=24") as ws:
            ws.send_bytes(b"id\r")
            out["host_echo"] = ws.receive_bytes()
        return out

    out = await asyncio.to_thread(scenario)
    assert out["unauth"] == 4401 and out["unauth_host"] == 4401
    assert out["echo"] == b"echo:uptime\r"
    assert term_ssh.opened[0] == ("docker exec -it dc1-spine1 bash", 100, 30, "xterm-256color")
    assert out["ghost"] == 4404 and b"not a node" in out["ghost_msg"]
    assert out["not_deployed"] == 4409 and out["bad_shell"] == 4400
    assert out["host_echo"] == b"echo:id\r" and term_ssh.opened[-1] == ("$SHELL -l", 80, 24, "xterm-256color")
