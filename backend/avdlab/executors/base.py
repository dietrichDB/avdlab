"""Executor interface + shared result types and helpers.

An executor turns a built lab into running devices on a host and back (deploy / destroy / status / validate).
`get_executor(lab)` picks the implementation from `Lab.target`: "clab" → `ClabExecutor`, "act" → `ActExecutor`.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Protocol, TypedDict

from pydantic import BaseModel, Field

from ..core.db import Host, Lab

LogFn = Callable[[str], None]
STATUSES = ("success", "failure", "error", "skipped", "unset")


class ExecContext(Protocol):
    """What executors need from a job context (JobContext satisfies it; InlineContext is used by sync endpoints)."""

    job_id: str
    params: dict[str, Any]

    def log(self, line: str) -> None: ...

    def check_cancelled(self) -> None: ...


class InlineContext:
    """Minimal context for work executed inside a request (e.g. GET /labs/{id}/status)."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.job_id = ""
        self.params = params or {}
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(str(line))

    def check_cancelled(self) -> None:
        return None


class DeployParams(BaseModel):
    reconfigure: bool = True
    wait_eapi: bool = True
    wait_timeout: int = Field(default=600, ge=10, le=7200)


class ValidateParams(BaseModel):
    extra_fabric_validation: bool = True
    skip_tests: list[str] = Field(default_factory=lambda: ["VerifyNTP", "VerifyReloadCause"])
    user_catalogs: bool = False
    tags: list[str] = Field(default_factory=list)


class NodeStatus(TypedDict):
    name: str
    kind: str
    state: str
    mgmt_ip: str | None
    image: str
    eapi: bool | None  # None for non-EOS nodes


class DeployResult(TypedDict):
    nodes: list[NodeStatus]
    ready: list[str]
    pending: list[str]
    duration: float


class LabStatus(TypedDict):
    deployed: bool
    nodes: list[NodeStatus]


class ValidationSummary(TypedDict):
    totals: dict[str, int]
    by_device: dict[str, dict[str, int]]
    report_paths: dict[str, str]
    validation_run_id: str
    ansible_rc: int


class ExecutorError(RuntimeError):
    pass


class Executor(Protocol):
    async def deploy(self, ctx: ExecContext, lab: Lab, host: Host) -> DeployResult: ...

    async def destroy(self, ctx: ExecContext, lab: Lab, host: Host) -> None: ...

    async def status(self, ctx: ExecContext, lab: Lab, host: Host, *, session: Any = None) -> LabStatus: ...

    async def validate(self, ctx: ExecContext, lab: Lab, host: Host, *, tags: list[str], skip_tests: list[str],
                       extra_fabric: bool, user_catalogs: bool = False) -> ValidationSummary: ...


def get_executor(lab: Lab) -> Executor:
    target = (lab.target or "clab").lower()
    if target == "clab":
        from .clab import ClabExecutor

        return ClabExecutor()
    if target == "act":
        from .act import ActExecutor

        return ActExecutor()
    raise ExecutorError(f"unknown lab target {lab.target!r}")


# ------------------------------------------------------------------------------------------ ANTA report parsing
def parse_anta_results(results: Any) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """ANTA JSON report (list of TestResult dicts) → (totals per status incl. `total`, per device totals)."""
    if isinstance(results, dict):  # tolerate {"results": [...]} wrappers
        results = results.get("results") or results.get("test_results") or []
    if not isinstance(results, list):
        raise ExecutorError("ANTA JSON report is not a list of results")
    totals: Counter[str] = Counter({s: 0 for s in STATUSES})
    by_device: dict[str, Counter[str]] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        status = str(r.get("result") or "unset").lower()
        device = str(r.get("name") or r.get("device") or "?")
        totals[status] += 1
        by_device.setdefault(device, Counter({s: 0 for s in STATUSES}))[status] += 1
    out_totals = dict(totals)
    out_totals["total"] = sum(totals.values())
    out_dev = {d: {**dict(c), "total": sum(c.values())} for d, c in sorted(by_device.items())}
    return out_totals, out_dev
