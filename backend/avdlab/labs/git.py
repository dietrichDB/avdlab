"""Per-lab git remote (phase 3 §2): remote config, push / pull / clone with run-time-only credentials.

`lab.settings["git"] = {url, branch, auth: {kind: none|token|ssh_key, secret_id}, last_sync: {at, direction, ok, message}}`

Credentials never reach `.git/config`, the stored URL, a command line or the job log:
* token   → a throw-away `GIT_ASKPASS` script answers the username prompt with `x-access-token` (GitHub, default) or
            `oauth2` (GitLab) and the password prompt by reading the token from a 0600 temp file;
* ssh_key → the private key in a 0600 temp file + `GIT_SSH_COMMAND="ssh -i <f> -o StrictHostKeyChecking=accept-new …"`.
Both temp files live in a private temp dir removed in `finally`. `credential.helper` is disabled per command so no
helper can persist the secret.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from git import Git, GitCommandError, Repo
from sqlalchemy import update

from ..core.config import settings
from ..core.db import Job, Lab, LabState, SessionLocal
from ..core.security import secret_store
from ..jobs.engine import JobContext, job
from . import service

AuthKind = Literal["none", "token", "ssh_key"]
AUTH_KINDS = ("none", "token", "ssh_key")
Log = Callable[[str], None]
_SCP_RE = re.compile(r"^[\w.-]+@[\w.-]+:(?!//)")  # git@github.com:org/repo.git


class GitRemoteError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ url helpers
def check_url(url: str) -> str:
    """Accept https/http/ssh/git/file URLs, scp-like `user@host:path` and absolute local paths; refuse URLs that embed
    a password/token (credentials are configured separately and injected at run time)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    if _SCP_RE.match(url) or url.startswith("/"):
        return url
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http", "ssh", "git", "file"):
        raise ValueError(f"unsupported git URL scheme {parts.scheme!r} (use https://, ssh://, git@host:path or file://)")
    if parts.password is not None or (parts.scheme in ("https", "http") and parts.username):
        raise ValueError("do not put credentials in the URL: use auth_kind token (or ssh_key) instead")
    return url


def token_username(url: str) -> str:
    """Username paired with a token: `oauth2` for GitLab hosts, `x-access-token` otherwise (GitHub, Gitea, …)."""
    host = (urlsplit(url).hostname or "").lower()
    return "oauth2" if "gitlab" in host else "x-access-token"


def scrub(text: str, *secrets: str | None) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "********")
    return text


# ------------------------------------------------------------------------------------------------ credentials
@contextmanager
def auth_env(url: str, kind: str, secret: str | None) -> Iterator[dict[str, str]]:
    """Environment for one git invocation. Temp files (0600, private 0700 dir) are removed in `finally`."""
    env: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    tmp: str | None = None
    try:
        if kind == "token":
            if not secret:
                raise GitRemoteError("token auth configured but no token available")
            tmp = tempfile.mkdtemp(prefix="avdlab-git-")
            token_file = Path(tmp) / "token"
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secret)
            askpass = Path(tmp) / "askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                f"  Username*|username*) echo {shlex.quote(token_username(url))} ;;\n"
                f"  *) cat {shlex.quote(str(token_file))} ;;\n"
                "esac\n"
            )
            askpass.chmod(stat.S_IRWXU)
            env.update({"GIT_ASKPASS": str(askpass), "SSH_ASKPASS": str(askpass)})
        elif kind == "ssh_key":
            if not secret:
                raise GitRemoteError("ssh_key auth configured but no key available")
            tmp = tempfile.mkdtemp(prefix="avdlab-git-")
            key_file = Path(tmp) / "id_key"
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(secret if secret.endswith("\n") else secret + "\n")
            known = settings.data_dir / "git_known_hosts"
            env["GIT_SSH_COMMAND"] = (f"ssh -i {shlex.quote(str(key_file))} -o StrictHostKeyChecking=accept-new "
                                      f"-o IdentitiesOnly=yes -o UserKnownHostsFile={shlex.quote(str(known))} "
                                      "-o BatchMode=yes")
        yield env
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


NO_HELPER = ["-c", "credential.helper="]


def _git(repo: Repo, env: dict[str, str], *args: str) -> str:
    """Run `git -c credential.helper= <args>` in the repo with `env` (never logged)."""
    return repo.git.execute(["git", *NO_HELPER, *args], env=env)  # type: ignore[call-overload]


# ------------------------------------------------------------------------------------------------ config
def lab_git(lab: Lab) -> dict[str, Any] | None:
    cfg = (lab.settings or {}).get("git")
    return cfg if isinstance(cfg, dict) and cfg.get("url") else None


def configure_remote(root: Path, url: str) -> None:
    repo = Repo(root)
    if "origin" in [r.name for r in repo.remotes]:
        repo.remote("origin").set_url(url)
    else:
        repo.create_remote("origin", url)


def remove_remote(root: Path) -> None:
    repo = Repo(root)
    if "origin" in [r.name for r in repo.remotes]:
        repo.delete_remote(repo.remote("origin"))


def ahead_behind(root: Path, branch: str) -> tuple[int, int]:
    """(ahead, behind) of HEAD vs `origin/<branch>` as of the last fetch/push (no network)."""
    repo = Repo(root)
    if not repo.head.is_valid():
        return 0, 0
    ref = f"refs/remotes/origin/{branch}"
    try:
        repo.git.rev_parse("--verify", "--quiet", ref)
    except GitCommandError:
        return int(repo.git.rev_list("--count", "HEAD")), 0
    out = repo.git.rev_list("--left-right", "--count", f"HEAD...{ref}")
    a, b = out.split()
    return int(a), int(b)


def git_status_out(lab: Lab) -> dict[str, Any]:
    cfg = lab_git(lab)
    if cfg is None:
        return {"configured": False, "url": None, "branch": None, "auth_kind": "none", "has_secret": False,
                "ahead": 0, "behind": 0, "last_sync": None}
    branch = cfg.get("branch") or "main"
    try:
        ahead, behind = ahead_behind(service.lab_dir(lab), branch)
    except Exception:  # noqa: BLE001 - missing dir / corrupt repo → zeros
        ahead, behind = 0, 0
    auth = cfg.get("auth") or {}
    return {"configured": True, "url": cfg["url"], "branch": branch, "auth_kind": auth.get("kind", "none"),
            "has_secret": bool(auth.get("secret_id")), "ahead": ahead, "behind": behind,
            "last_sync": cfg.get("last_sync")}


async def resolve_secret(cfg: dict[str, Any]) -> str | None:
    auth = cfg.get("auth") or {}
    if auth.get("kind", "none") == "none" or not auth.get("secret_id"):
        return None
    async with SessionLocal() as s:
        try:
            return await secret_store.reveal(s, auth["secret_id"])
        except KeyError as exc:
            raise GitRemoteError("the git credential secret no longer exists: reconfigure the remote") from exc


# ------------------------------------------------------------------------------------------------ operations (sync)
def push(root: Path, url: str, branch: str, kind: str, secret: str | None, *, force: bool = False, log: Log = print) -> str:
    repo = Repo(root)
    service.ensure_gitignore(root)
    service.commit_all(root, "avdlab: .gitignore")
    with auth_env(url, kind, secret) as env:
        args = ["push", "--porcelain"] + (["--force"] if force else []) + ["origin", f"HEAD:refs/heads/{branch}"]
        try:
            out = _git(repo, env, *args)
        except GitCommandError as exc:
            raise GitRemoteError(scrub(f"git push failed: {(exc.stderr or exc.stdout or '').strip()}", secret)) from None
        try:  # refresh origin/<branch> so ahead/behind is exact
            _git(repo, env, "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        except GitCommandError:
            pass
    log(scrub(out.strip() or "pushed", secret))
    return repo.head.commit.hexsha


def pull(root: Path, url: str, branch: str, kind: str, secret: str | None, *, log: Log = print) -> dict[str, Any]:
    """Fetch + `merge --ff-only`. Returns {old, new, changed_files, inputs_changed}."""
    repo = Repo(root)
    if repo.is_dirty(untracked_files=True):
        raise GitRemoteError("the lab has uncommitted changes: refusing to pull")
    old = repo.head.commit.hexsha if repo.head.is_valid() else None
    with auth_env(url, kind, secret) as env:
        try:
            _git(repo, env, "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        except GitCommandError as exc:
            raise GitRemoteError(scrub(f"git fetch failed: {(exc.stderr or exc.stdout or '').strip()}", secret)) from None
    try:
        repo.git.merge("--ff-only", f"origin/{branch}")
    except GitCommandError as exc:
        raise GitRemoteError("cannot fast-forward: local and remote history diverged "
                             f"({(exc.stderr or '').strip().splitlines()[-1:] or ''}); push with force or resolve manually") from None
    new = repo.head.commit.hexsha
    changed = repo.git.diff("--name-only", old, new).splitlines() if old and old != new else []
    inputs = [f for f in changed if service.is_knob_managed(f) or f.startswith("host_vars/")
              or f in ("inventory.yaml", "hosts.yml")]
    log(f"fast-forwarded {old[:8] if old else '-'} → {new[:8]} ({len(changed)} files)")
    return {"old": old, "new": new, "changed_files": changed, "inputs_changed": bool(inputs)}


def clone(url: str, dest: Path, branch: str | None, kind: str, secret: str | None, *, log: Log = print) -> str:
    """Clone into `dest` (must not exist). Returns the checked-out branch."""
    with auth_env(url, kind, secret) as env:
        args = ["clone", "--origin", "origin"] + (["--branch", branch] if branch else []) + ["--", url, str(dest)]
        try:
            Git().execute(["git", *NO_HELPER, *args], env=env)  # type: ignore[call-overload]
        except GitCommandError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitRemoteError(scrub(f"git clone failed: {(exc.stderr or exc.stdout or '').strip()}", secret)) from None
    repo = Repo(dest)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", service.GIT_AUTHOR.name)
        cw.set_value("user", "email", service.GIT_AUTHOR.email)
    active = repo.active_branch.name
    log(f"cloned {url} ({active}) into {dest.name}")
    return active


# ------------------------------------------------------------------------------------------------ jobs
async def _record_sync(lab_id: str, direction: str, ok: bool, message: str) -> None:
    async with SessionLocal() as s:
        row = await s.get(Lab, lab_id)
        if row is None:
            return
        st = dict(row.settings or {})
        cfg = dict(st.get("git") or {})
        cfg["last_sync"] = {"at": time.time(), "direction": direction, "ok": ok, "message": message[:500]}
        st["git"] = cfg
        row.settings = st
        await s.commit()


@job("lab.git_push")
async def lab_git_push(ctx: JobContext) -> dict[str, Any]:
    lab = await ctx.get_lab()
    cfg = lab_git(lab)
    if cfg is None:
        raise GitRemoteError("no git remote configured for this lab")
    branch = cfg.get("branch") or "main"
    kind = (cfg.get("auth") or {}).get("kind", "none")
    force = bool((ctx.params or {}).get("force"))
    ctx.log(f"[git] push HEAD → {cfg['url']} {branch} (auth {kind}{', force' if force else ''})")
    try:
        secret = await resolve_secret(cfg)
        sha = await asyncio.to_thread(push, service.lab_dir(lab), cfg["url"], branch, kind, secret, force=force, log=ctx.log)
    except Exception as exc:
        await _record_sync(lab.id, "push", False, str(exc))
        raise
    await _record_sync(lab.id, "push", True, f"pushed {sha[:8]} to {branch}")
    ahead, behind = await asyncio.to_thread(ahead_behind, service.lab_dir(lab), branch)
    return {"commit": sha, "branch": branch, "ahead": ahead, "behind": behind}


async def revalidate(lab_id: str, root: Path, log: Log) -> list[dict[str, Any]]:
    """pyavd validation of the lab inputs (after a pull / import). Updates custom labs' fabric_name."""
    from .archive import detect_fabric_name, validate_dir

    violations = await asyncio.to_thread(validate_dir, root)
    for v in violations[:20]:
        log(f"[violation] {v.get('host', '')}: {v.get('path', '')}: {v['message']}")
    async with SessionLocal() as s:
        row = await s.get(Lab, lab_id)
        if row is not None:
            st = dict(row.settings or {})
            if row.template == "custom":
                st["fabric_name"] = await asyncio.to_thread(detect_fabric_name, root)
            st["import_warnings"] = violations
            row.settings = st
            await s.commit()
    return violations


@job("lab.git_pull")
async def lab_git_pull(ctx: JobContext) -> dict[str, Any]:
    lab = await ctx.get_lab()
    cfg = lab_git(lab)
    if cfg is None:
        raise GitRemoteError("no git remote configured for this lab")
    branch = cfg.get("branch") or "main"
    kind = (cfg.get("auth") or {}).get("kind", "none")
    root = service.lab_dir(lab)
    ctx.log(f"[git] pull {cfg['url']} {branch} --ff-only (auth {kind})")
    try:
        secret = await resolve_secret(cfg)
        res = await asyncio.to_thread(pull, root, cfg["url"], branch, kind, secret, log=ctx.log)
    except Exception as exc:
        await _record_sync(lab.id, "pull", False, str(exc))
        raise
    violations: list[dict[str, Any]] = []
    if res["inputs_changed"]:
        ctx.log("[git] inventory/group_vars changed: re-validating with pyavd, lab back to draft")
        violations = await revalidate(lab.id, root, ctx.log)
        async with SessionLocal() as s:
            row = await s.get(Lab, lab.id)
            if row is not None and row.state == LabState.built:
                row.state = LabState.draft
                await s.commit()
            elif row is not None and row.state in (LabState.deployed, LabState.validated):
                ctx.log("[git] lab is deployed: rebuild + redeploy to apply the pulled inputs")
    await _record_sync(lab.id, "pull", True, f"{(res['old'] or '-')[:8]} → {res['new'][:8]}, {len(res['changed_files'])} files")
    ahead, behind = await asyncio.to_thread(ahead_behind, root, branch)
    return {**res, "violations": violations, "ahead": ahead, "behind": behind}


@job("lab.import_git")
async def lab_import_git(ctx: JobContext) -> dict[str, Any]:
    """params: {name, slug, url, branch|null, auth: {kind, secret_id}, host_id|null}. Creates the Lab row on success."""
    from .archive import create_custom_lab

    p = ctx.params or {}
    auth = p.get("auth") or {"kind": "none", "secret_id": None}
    cfg = {"url": p["url"], "branch": p.get("branch"), "auth": auth}
    secret = await resolve_secret(cfg)
    dest = service.lab_dir(p["slug"])
    if dest.exists():
        raise GitRemoteError(f"lab directory {dest.name} already exists")
    ctx.log(f"[git] clone {p['url']} {p.get('branch') or '(default branch)'} (auth {auth.get('kind', 'none')})")
    branch = await asyncio.to_thread(clone, p["url"], dest, p.get("branch") or None, auth.get("kind", "none"), secret,
                                     log=ctx.log)
    try:
        lab, warnings = await create_custom_lab(name=p["name"], slug=p["slug"], host_id=p.get("host_id"),
                                                source="git", log=ctx.log,
                                                git_cfg={"url": p["url"], "branch": branch, "auth": auth})
    except Exception:
        await asyncio.to_thread(service.delete_lab_dir, p["slug"])
        raise
    async with SessionLocal() as s:
        await s.execute(update(Job).where(Job.id == ctx.job_id).values(lab_id=lab.id))
        await s.commit()
    await _record_sync(lab.id, "pull", True, f"cloned {branch}")
    return {"lab_id": lab.id, "slug": lab.slug, "branch": branch, "fabric_name": (lab.settings or {}).get("fabric_name"),
            "import_warnings": warnings}
