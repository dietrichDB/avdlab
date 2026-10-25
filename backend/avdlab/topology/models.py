"""Pydantic models for the containerlab topology file and the UI graph."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ClabNode(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: str
    image: str | None = None
    mgmt_ipv4: str | None = Field(default=None, alias="mgmt-ipv4")
    mgmt_ipv6: str | None = Field(default=None, alias="mgmt-ipv6")  # phase 6: ipv6_mgmt_ip (dual-stack OOB)
    startup_config: str | None = Field(default=None, alias="startup-config")
    binds: list[str] = []
    exec: list[str] = []
    env: dict[str, str] = {}
    labels: dict[str, str] = {}
    # phase 4 (A, ZTP): unset/empty on every node in startup-config mode → dropped from the YAML
    suppress_startup_config: bool | None = Field(default=None, alias="suppress-startup-config")
    user: str | None = None
    cap_add: list[str] = Field(default=[], alias="cap-add")


class ClabLink(BaseModel):
    endpoints: list[str]


class ClabKind(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    image: str | None = None
    enforce_startup_config: bool | None = Field(default=None, alias="enforce-startup-config")


class ClabTopologySection(BaseModel):
    kinds: dict[str, ClabKind] = {}
    nodes: dict[str, ClabNode] = {}
    links: list[ClabLink] = []


class ClabMgmt(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    network: str
    ipv4_subnet: str | None = Field(default=None, alias="ipv4-subnet")
    ipv6_subnet: str | None = Field(default=None, alias="ipv6-subnet")  # phase 6: only when nodes have mgmt-ipv6


class ClabTopology(BaseModel):
    """`topology.clab.yml` as data. `to_yaml_dict()` uses containerlab's dashed keys."""

    name: str
    prefix: str = ""
    mgmt: ClabMgmt
    topology: ClabTopologySection

    def to_yaml_dict(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True, exclude_defaults=False)
        for node in data["topology"]["nodes"].values():  # drop empty binds/exec/env/labels for a readable file
            for key in [k for k, v in node.items() if v in ([], {})]:
                del node[key]
        return data

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any]) -> "ClabTopology":
        return cls.model_validate(data)


class GraphNode(BaseModel):
    name: str
    kind: str  # arista_ceos | linux
    role: str | None = None  # spine | l3leaf | l2leaf | server | ...
    mgmt_ip: str | None = None
    mgmt_ipv6: str | None = None
    serial: str | None = None
    image: str | None = None


class GraphLink(BaseModel):
    a: str
    a_if: str
    b: str
    b_if: str
    a_port: str | None = None  # containerlab port names (ethN)
    b_port: str | None = None
    type: str | None = None  # peer type from the topology CSV (spine, mlag_peer, l2leaf, server ...)


class TopologyGraph(BaseModel):
    lab_id: str
    name: str
    mgmt_subnet: str | None
    mgmt_ipv6_subnet: str | None = None
    nodes: list[GraphNode]
    links: list[GraphLink]
    layout: dict[str, Any] | None = None  # phase 5: {positions: {node: {x, y}}, locked} from clab/layout.json
