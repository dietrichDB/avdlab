"""asyncssh wrapper used by every host job.

    async with SSHClient.connect(host_row, session) as ssh:
        cp = await ssh.run("docker version", sudo=False, log=ctx.log)
        await ssh.put(local, remote); await ssh.get(remote, local)
        await ssh.rsync_dir(local_dir, remote_dir, exclude=[".git"])

Credentials are resolved from the secret store (`ssh_key_secret_id` → private key text, `password_secret_id` → password).
Host keys use trust-on-first-use: unknown hosts are recorded in `<data_dir>/known_hosts`; a later mismatch fails the connection.
Tests replace `SSHClient.connect` with a fake context manager yielding an object implementing `CommandRunner`.
"""
from __future__ import annotations

import asyncio
import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol, Sequence

import asyncssh
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db import Host
from ..core.security import secret_store

LogFn = Callable[[str], None]


@dataclass
class CompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    command: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def check(self) -> "CompletedProcess":
        if not self.ok:
            raise SSHCommandError(self)
        return self


class SSHError(RuntimeError):
    pass


class SSHCommandError(SSHError):
    def __init__(self, cp: CompletedProcess) -> None:
        self.cp = cp
        tail = (cp.stderr or cp.stdout).strip().splitlines()[-3:]
        super().__init__(f"command failed rc={cp.returncode}: {cp.command!r} :: {' | '.join(tail)}")


class CommandRunner(Protocol):
    """Minimal interface the probe/recipe code depends on (fake SSH implements exactly this)."""

    async def run(self, cmd: str, *, sudo: bool = False, timeout: float | None = 600, log: LogFn | None = None,
                  input: str | None = None) -> CompletedProcess: ...


SUDO_HINT = ("sudo needs a password on this host: either add a sudo password to the host (Hosts → edit → 'sudo password'), "
             "or grant NOPASSWD: echo \"$USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/avdlab")


def sudo_wrap(cmd: str, *, with_password: bool = False) -> str:
    """Run `cmd` through a root shell. Without a password `-n` makes a missing NOPASSWD rule fail fast instead of
    hanging; with a password `-S -p ''` reads it from stdin (first line) without echoing a prompt."""
    flags = "-S -p ''" if with_password else "-n"
    return f"sudo {flags} bash -c {shlex.quote(cmd)}"


def is_sudo_auth_error(stderr: str) -> bool:
    return "interactive authentication is required" in stderr or "a password is required" in stderr or "incorrect password" in stderr


# ------------------------------------------------------------------ known_hosts (TOFU)
def known_hosts_path() -> Path:
    return settings.data_dir / "known_hosts"


def _kh_host_pattern(address: str, port: int) -> str:
    return address if port == 22 else f"[{address}]:{port}"


def _kh_read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _kh_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
    path.chmod(0o600)


def parse_jump(spec: str | None, default_user: str, default_port: int = 22) -> tuple[str, str, int] | None:
    """'[user@]host[:port]' → (user, host, port); None when empty."""
    s = (spec or "").strip()
    if not s:
        return None
    user = default_user
    if "@" in s:
        user, s = s.split("@", 1)
    port = default_port
    if ":" in s and not s.startswith("["):
        s, p = s.rsplit(":", 1)
        port = int(p)
    return user or default_user, s, port


async def ensure_known_host(address: str, port: int, log: LogFn | None = None, *, tunnel: Any = None) -> Path:
    """Trust-on-first-use: if the host is not in our known_hosts, fetch its key once and pin it (through `tunnel` when
    the host sits behind a jump host)."""
    path = known_hosts_path()
    data = await asyncio.to_thread(_kh_read, path)
    if data.strip():
        matched = asyncssh.match_known_hosts(asyncssh.import_known_hosts(data), address, address, port)
        if matched[0]:  # host keys present → pinned already
            return path
    key = await asyncssh.get_server_host_key(address, port, tunnel=tunnel)
    if key is None:
        raise SSHError(f"could not fetch host key from {address}:{port}")
    line = f"{_kh_host_pattern(address, port)} {key.export_public_key('openssh').decode().strip()}"
    await asyncio.to_thread(_kh_append, path, line)
    if log:
        log(f"[tofu] pinned host key {key.get_fingerprint()} for {address}:{port}")
    return path


CONNECT_ATTEMPTS = 3  # transient TCP/handshake failures are retried with backoff (tests set 1)
CONNECT_BACKOFF = 3.0


def _describe_connect_error(exc: BaseException) -> str:
    """Bare `TimeoutError`/`OSError` have empty messages; say what actually happened."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or exc.__class__.__name__ == "TimeoutError":
        return "TCP connect timed out — host unreachable from the app container (host asleep/offline, firewall, or the Docker network lost LAN access; try `docker compose restart api`)"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused — sshd not listening on that port"
    if isinstance(exc, asyncssh.PermissionDenied):
        return f"authentication failed ({exc})"
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return f"host key mismatch ({exc}) — remove the host's line from <data>/known_hosts if the host was reinstalled"
    return str(exc) or exc.__class__.__name__


async def _connect_with_retry(host: Host, kwargs: dict[str, object], log: LogFn | None, *, address: str | None = None,
                              port: int | None = None, label: str = "") -> asyncssh.SSHClientConnection:
    address = address or host.address
    port = port or host.ssh_port
    last: BaseException | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return await asyncssh.connect(address, port, **kwargs)  # type: ignore[arg-type]
        except asyncssh.PermissionDenied as exc:  # retrying a bad credential only locks accounts
            raise SSHError(f"ssh {label}{kwargs.get('username')}@{address}:{port}: {_describe_connect_error(exc)}") from exc
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            last = exc
            if attempt < CONNECT_ATTEMPTS:
                if log:
                    log(f"[ssh] connect to {label}{address}:{port} failed (attempt {attempt}/{CONNECT_ATTEMPTS}): "
                        f"{_describe_connect_error(exc)}; retrying in {CONNECT_BACKOFF:.0f}s")
                await asyncio.sleep(CONNECT_BACKOFF)
    raise SSHError(f"ssh {label}{kwargs.get('username')}@{address}:{port}: {_describe_connect_error(last)}"  # type: ignore[arg-type]
                   f" (after {CONNECT_ATTEMPTS} attempts)") from last


# ------------------------------------------------------------------ client
class SSHClient:
    def __init__(self, conn: asyncssh.SSHClientConnection, host: Host, sudo_password: str | None = None) -> None:
        self._conn = conn
        self.host = host
        self._sudo_password = sudo_password
        self._sftp: asyncssh.SFTPClient | None = None
        self._jump: asyncssh.SSHClientConnection | None = None

    # ---- connection
    @classmethod
    @asynccontextmanager
    async def connect(cls, host: Host, session: AsyncSession, *, log: LogFn | None = None,
                      connect_timeout: float = 30, ctx: Any = None) -> AsyncIterator["SSHClient"]:
        """`ctx` (a JobContext): the client is registered with `ctx.attach_ssh()` while open so killing the job
        closes the connection (and with it every remote command)."""
        kwargs: dict[str, object] = {"username": host.ssh_user, "connect_timeout": connect_timeout}
        if host.ssh_key_secret_id:
            pem = await secret_store.reveal(session, host.ssh_key_secret_id)
            kwargs["client_keys"] = [asyncssh.import_private_key(pem)]
        elif host.password_secret_id:
            kwargs["password"] = await secret_store.reveal(session, host.password_secret_id)
        else:
            raise SSHError(f"host {host.name} has neither ssh key nor password secret")
        sudo_password: str | None = None
        if host.sudo_password_secret_id:
            sudo_password = await secret_store.reveal(session, host.sudo_password_secret_id)
        elif host.password_secret_id:
            sudo_password = await secret_store.reveal(session, host.password_secret_id)
        jump = parse_jump(getattr(host, "jump_host", None), host.ssh_user)
        jump_conn: asyncssh.SSHClientConnection | None = None
        if jump:  # 1.0.1: hop through the Mac / Windows machine in front of its Linux VM, same credentials on both
            juser, jaddr, jport = jump
            jkw = {**kwargs, "username": juser, "known_hosts": str(await ensure_known_host(jaddr, jport, log))}
            jump_conn = await _connect_with_retry(host, jkw, log, address=jaddr, port=jport, label="jump ")
            if log:
                log(f"[ssh] via jump host {juser}@{jaddr}:{jport}")
            kwargs["tunnel"] = jump_conn
        try:
            kwargs["known_hosts"] = str(await ensure_known_host(host.address, host.ssh_port, log, tunnel=jump_conn))
            conn = await _connect_with_retry(host, kwargs, log)
        except BaseException:
            if jump_conn is not None:
                jump_conn.close()
            raise
        client = cls(conn, host, sudo_password)
        client._jump = jump_conn
        tracked = ctx is not None and hasattr(ctx, "attach_ssh")  # InlineContext (sync endpoints) has no job to kill
        if tracked:
            ctx.attach_ssh(client)
        try:
            yield client
        finally:
            if tracked:
                ctx.detach_ssh(client)
            await client.close()

    async def close(self) -> None:
        if self._sftp is not None:
            self._sftp.exit()
            self._sftp = None
        self._conn.close()  # idempotent: a killed job's connection is closed twice (cancel + context exit)
        await self._conn.wait_closed()
        jump = getattr(self, "_jump", None)
        if jump is not None:
            jump.close()
            await jump.wait_closed()

    # ---- commands
    async def run(self, cmd: str, *, sudo: bool = False, timeout: float | None = 600, log: LogFn | None = None,
                  input: str | None = None) -> CompletedProcess:
        use_pw = sudo and self._sudo_password is not None
        full = sudo_wrap(cmd, with_password=use_pw) if sudo else cmd
        if use_pw:
            input = self._sudo_password + "\n" + (input or "")
        out: list[str] = []
        err: list[str] = []

        async def pump(reader: asyncssh.SSHReader[str], sink: list[str], prefix: str) -> None:
            async for line in reader:
                if not line:  # asyncssh yields '' once at EOF
                    continue
                sink.append(line)
                if log:
                    log(prefix + line.rstrip("\n"))

        try:
            async with asyncio.timeout(timeout):
                proc = await self._conn.create_process(full, term_type=None)
                if input is not None:
                    proc.stdin.write(input)
                    proc.stdin.write_eof()
                await asyncio.gather(pump(proc.stdout, out, ""), pump(proc.stderr, err, "! "))
                await proc.wait()
        except TimeoutError as exc:
            raise SSHError(f"timeout after {timeout}s: {cmd!r}") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"ssh error running {cmd!r}: {exc}") from exc
        rc = proc.exit_status if proc.exit_status is not None else -1
        stderr = "".join(err)
        if sudo and rc != 0 and is_sudo_auth_error(stderr):
            raise SSHError(f"sudo failed on {self.host.name}: {stderr.strip().splitlines()[-1] if stderr.strip() else 'authentication required'}. {SUDO_HINT}")
        return CompletedProcess(rc, "".join(out), stderr, command=cmd)

    # ---- interactive (web terminal)
    async def open_pty(self, cmd: str | None, *, cols: int = 80, rows: int = 24,
                       term_type: str = "xterm-256color") -> asyncssh.SSHClientProcess[bytes]:
        """Start `cmd` (None = the login shell) on a PTY and return the raw asyncssh process (bytes in/out,
        stderr merged by the PTY). The caller owns it: `process.close()` when done."""
        try:
            return await self._conn.create_process(cmd, term_type=term_type, term_size=(cols, rows), encoding=None)
        except asyncssh.Error as exc:
            raise SSHError(f"ssh error opening a terminal: {exc}") from exc

    # ---- files
    async def _sftp_client(self) -> asyncssh.SFTPClient:
        if self._sftp is None:
            self._sftp = await self._conn.start_sftp_client()
        return self._sftp

    async def put(self, local: str | Path, remote: str) -> None:
        sftp = await self._sftp_client()
        await sftp.put(str(local), remote)

    async def get(self, remote: str, local: str | Path) -> None:
        sftp = await self._sftp_client()
        await sftp.get(remote, str(local))

    async def rsync_dir(self, local_dir: str | Path, remote_dir: str, *, exclude: Sequence[str] = (), delete: bool = False,
                        log: LogFn | None = None) -> None:
        """Copy a directory tree with a `tar | ssh tar` pipe over the existing connection.

        rsync would need the private key on disk for a separate ssh process, so the tar pipe is used unconditionally;
        `delete=True` gives rsync `--delete` semantics by clearing the remote tree first.
        """
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise SSHError(f"not a directory: {local_dir}")
        tar_cmd = ["tar", "-C", str(local_dir), "-czf", "-"]
        for pat in exclude:
            tar_cmd.append(f"--exclude={pat}")
        tar_cmd.append(".")
        pre = f"rm -rf {shlex.quote(remote_dir)} && " if delete else ""
        remote_cmd = f"{pre}mkdir -p {shlex.quote(remote_dir)} && tar -C {shlex.quote(remote_dir)} -xzf -"
        local_proc = await asyncio.create_subprocess_exec(*tar_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert local_proc.stdout is not None
        proc = await self._conn.create_process(remote_cmd, stdin=local_proc.stdout, encoding=None)
        result = await proc.wait()
        await local_proc.wait()
        if local_proc.returncode != 0:
            err = (await local_proc.stderr.read()).decode() if local_proc.stderr else ""
            raise SSHError(f"local tar failed: {err.strip()}")
        if result.exit_status != 0:
            raise SSHError(f"remote extract failed rc={result.exit_status}: {(result.stderr or b'')[-300:]!r}")
        if log:
            log(f"[sync] {local_dir} -> {self.host.address}:{remote_dir}")
