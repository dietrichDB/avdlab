"""Thin async wrapper around the local docker CLI and other subprocesses.

Everything that touches docker or spawns `ardl` goes through `backend` (a module-level `LocalDocker` instance), so
tests replace it with a fake: `monkeypatch.setattr(avdlab.images.docker, "backend", FakeDocker())`.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

LogFn = Callable[[str], None]


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerError(RuntimeError):
    pass


async def run_process(argv: Sequence[str], *, env: dict[str, str] | None = None, log: LogFn | None = None,
                      cwd: Path | None = None, timeout: float | None = None,
                      redact: Sequence[str] = ()) -> ProcResult:
    """Run a local process, streaming merged output lines to `log` (with `redact` strings masked)."""

    def clean(line: str) -> str:
        for secret in redact:
            if secret:
                line = line.replace(secret, "***")
        return line

    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
    )
    out: list[str] = []

    async def pump() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = clean(raw.decode(errors="replace").rstrip("\n"))
            out.append(line)
            if log:
                log(line)

    try:
        async with asyncio.timeout(timeout):
            await pump()
            await proc.wait()
    except TimeoutError:
        proc.kill()
        raise DockerError(f"timeout after {timeout}s: {argv[0]}") from None
    return ProcResult(proc.returncode or 0, "\n".join(out), "")


class LocalDocker:
    """docker CLI on the API host. `available()` is False when docker is not installed/reachable."""

    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    async def available(self) -> bool:
        if not shutil.which(self.binary):
            return False
        try:
            res = await run_process([self.binary, "version", "--format", "{{.Server.Version}}"], timeout=15)
        except (OSError, DockerError):
            return False
        return res.ok

    async def image_exists(self, tag: str) -> bool:
        res = await run_process([self.binary, "image", "inspect", tag], timeout=30)
        return res.ok

    async def import_tarball(self, path: Path, tag: str, *, log: LogFn | None = None) -> None:
        """`docker import cEOS-lab-<ver>.tar.xz arista/ceos:<ver>` (cEOS images are rootfs tarballs)."""
        res = await run_process([self.binary, "import", str(path), tag], log=log, timeout=1800)
        if not res.ok:
            raise DockerError(f"docker import failed ({res.returncode}): {res.stdout[-500:]}")

    async def save(self, tag: str, dest: Path, *, log: LogFn | None = None) -> Path:
        """`docker save -o dest tag` (used to push an image that has no tarball on disk)."""
        res = await run_process([self.binary, "save", "-o", str(dest), tag], log=log, timeout=1800)
        if not res.ok:
            raise DockerError(f"docker save failed ({res.returncode}): {res.stdout[-500:]}")
        return dest

    async def run_cli(self, args: Sequence[str], *, log: LogFn | None = None, timeout: float | None = None) -> ProcResult:
        """Any docker CLI command (phase 6: the self-update helper container, `docker logs -f`)."""
        return await run_process([self.binary, *args], log=log, timeout=timeout)

    async def run_ardl(self, args: Sequence[str], *, token: str, log: LogFn | None = None,
                       cwd: Path | None = None) -> ProcResult:
        """Run eos-downloader's `ardl` with the token passed via ARISTA_TOKEN (never on the command line)."""
        binary = shutil.which("ardl") or "ardl"
        return await run_process([binary, *args], env={"ARISTA_TOKEN": token}, log=log, cwd=cwd, timeout=3600,
                                 redact=[token])


backend: LocalDocker = LocalDocker()


def get_backend() -> LocalDocker:
    """Always resolve through the module attribute so monkeypatching `backend` works everywhere."""
    return backend
