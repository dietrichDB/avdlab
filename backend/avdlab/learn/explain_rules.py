"""Rules table for the config explainer (AVD 6.4 `eos_designs` → `eos_cli_config_gen`).

A rule = (id, section regex | None for top-level lines, optional parent regex, line regex, handler). The first rule
whose scope and line regex match *and* whose handler returns an `Expl` wins, so specific rules come before generic
ones. Handlers read real values from the lab through `NodeCtx` (group_vars with provenance, knobs, structured config)
and compute the same formulas AVD uses (pyavd `get_ip_from_pool`), so explanations quote the lab's own numbers.

AVD references: eos_designs data models (`roles/eos_designs/docs/data-models.md`, tables under `docs/tables/`),
IP addressing (`pyavd/_eos_designs/shared_utils/…`, `pyavd/api/ip_addressing`), porting guide 6.x.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .explain import NOT_SET, ConfigLine, Expl, Handler, Input, Lines, NodeCtx
from .schema import doc_urls

M = re.Match


def D(table: str) -> str | None:
    return doc_urls("eos_designs", table)[0]


def C(table: str) -> str | None:
    return doc_urls("eos_cli_config_gen", table)[0]


def pool_ip(pool: str, prefixlen: int, subnet_offset: int, ip_offset: int) -> str | None:
    try:
        from pyavd._utils import get_ip_from_pool

        return get_ip_from_pool(str(pool), prefixlen, subnet_offset, ip_offset)
    except Exception:  # noqa: BLE001 - pool exhausted / malformed: rule falls back to a generic text
        return None


def ip_only(cidr: str) -> str:
    return str(cidr).split("/")[0]


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass
class Rule:
    id: str
    section: re.Pattern[str] | None
    line: re.Pattern[str]
    fn: Handler
    parent: re.Pattern[str] | None = None
    topic: str | None = None

    def applies(self, cl: ConfigLine, header: str, parent: str) -> bool:
        if self.section is None:
            return cl.indent == 0
        if cl.indent == 0 or not self.section.search(header):
            return False
        return not self.parent or bool(self.parent.search(parent))


RULES: list[Rule] = []


def rule(rid: str, line: str, section: str | None = None, parent: str | None = None, topic: str | None = None):
    def deco(fn: Handler) -> Handler:
        RULES.append(Rule(rid, re.compile(section) if section else None, re.compile(line), fn,
                          re.compile(parent) if parent else None, topic))
        return fn
    return deco


# ================================================================================================ system / management
@rule("hostname", r"^hostname (\S+)$", topic="system")
def _hostname(n: NodeCtx, m: M, cl, L) -> Expl:
    node = n.setting("name")
    key = node.key.rsplit(".", 1)[0] + ".name" if node.where != NOT_SET else f"{n.type_key}.nodes[].name"
    return Expl(f"The hostname is the Ansible inventory hostname '{n.host}'. AVD looks the device up by that name under "
                f"`{n.type_key}.nodes[]` / `{n.type_key}.node_groups[].nodes[]` (type `{n.type}`), so the node entry's "
                "`name` must equal the inventory hostname.",
                [Input(key, n.host, node.where if node.where != NOT_SET else "inventory.yml"),
                 Input("type", n.type, n.where_of("type"))],
                D("node-type-structure"), related=[r"^hostname "])


@rule("local-user", r"^username (\S+) privilege (\d+) role (\S+) (nopassword|secret sha512 .*)$", topic="system")
def _user(n: NodeCtx, m: M, cl, L) -> Expl:
    name = m.group(1)
    users = n.fabric("aaa_settings.local_users")
    knob = n.lab.knob_if("device_password")
    how = ("`no_password: true` → `nopassword`" if m.group(4) == "nopassword" else
           "`sha512_password` → `secret sha512 <hash>`" + (" (the lab's device_password knob is hashed into it)" if knob else ""))
    return Expl(f"Local user '{name}' comes from `aaa_settings.local_users[name={name}]` (privilege {m.group(2)}, role "
                f"{m.group(3)}); {how}. Since AVD 6.0 local users live under `aaa_settings` (the top-level "
                "`local_users` key was removed).",
                [Input(f"aaa_settings.local_users[name={name}]", next((u for u in users.value or [] if u.get("name") == name), None),
                       users.where, knob)], D("aaa-settings"), related=[r"^username "])


@rule("no-enable-password", r"^no (enable password|aaa root)$", topic="system")
def _noenable(n: NodeCtx, m: M, cl, L) -> Expl:
    if "enable" in m.group(1):
        return Expl("`no enable password`: eos_designs sets `enable_password.disabled: true` (EOS default) unless "
                    "`aaa_settings.enable_password.password` / `cleartext_password` is defined.",
                    [n.fabric("aaa_settings.enable_password")], D("aaa-settings"))
    return Expl("`no aaa root`: eos_designs renders `aaa_root.disabled: not aaa_settings.root_login.enabled` — the root "
                "account stays disabled unless you enable it (with a sha512_password).",
                [n.fabric("aaa_settings.root_login")], D("aaa-settings"))


@rule("vlan-internal-order", r"^vlan internal order (\S+) range (\d+) (\d+)$", topic="system")
def _vio(n: NodeCtx, m: M, cl, L) -> Expl:
    inp = n.fabric("internal_vlan_order", {"allocation": "ascending", "range": {"beginning": 1006, "ending": 1199}})
    return Expl(f"Internal VLANs (used by EOS for routed ports) are allocated {m.group(1)} from {m.group(2)} to "
                f"{m.group(3)}. This is `internal_vlan_order`; AVD always renders its default (ascending 1006-1199) so "
                "tenant VLANs must stay outside that range.", [inp], D("system-settings"))


@rule("transceiver", r"^transceiver qsfp default-mode 4x10G$", topic="system")
def _xcvr(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("eos_designs sets `transceiver_qsfp_default_mode_4x10: true` on every device except WAN routers (there "
                "is no input to change it yet). It matches the historic EOS default for QSFP ports; harmless on cEOS.",
                [Input("structured_config.transceiver_qsfp_default_mode_4x10", True, NOT_SET)],
                C("transceiver-qsfp-default-mode"))


@rule("routing-model", r"^service routing protocols model (\S+)$", topic="system")
def _model(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"`service routing protocols model {m.group(1)}` is always rendered by eos_designs: EVPN/VXLAN needs the "
                "multi-agent routing model (ArBGP). Changing it requires a reload on real hardware, which is why AVD "
                "sets it from day one. ANTA checks it with VerifyRoutingProtocolModel.",
                [Input("structured_config.service_routing_protocols_model", m.group(1), NOT_SET)],
                C("service-routing-protocols-model"))


@rule("dns-lookup", r"^ip domain lookup vrf (\S+) source-interface (\S+)$", topic="management")
def _dnslookup(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"DNS lookups are sourced from {m.group(2)} in VRF {m.group(1)} because the `dns_settings.servers` use the "
                "management VRF (`vrf: use_mgmt_interface_vrf`, the default) — AVD derives the VRF and source interface "
                "from `mgmt_interface_settings` / `default_mgmt_method: oob`.",
                [n.fabric("dns_settings"), Input("default_mgmt_method", n.fabric("default_mgmt_method", "oob").value, n.fabric("default_mgmt_method").where)],
                D("dns-settings"), related=[r"^ip name-server "])


@rule("name-server", r"^ip name-server vrf (\S+) (\S+)$", topic="management")
def _ns(n: NodeCtx, m: M, cl, L) -> Expl:
    inp = n.fabric("dns_settings.servers")
    inp.knob = n.lab.knob_if("dns_servers")
    return Expl(f"Resolver {m.group(2)} in VRF {m.group(1)} = `dns_settings.servers[ip_address={m.group(2)}]`"
                + (" (written from the lab's dns_servers knob)" if inp.knob else "") +
                ". Use public resolvers if devices must reach CVaaS.", [inp], D("dns-settings"),
                related=[r"^ip domain lookup"])


@rule("mgmt-vrf", r"^vrf instance (MGMT)$", topic="management")
def _mgmtvrf(n: NodeCtx, m: M, cl, L) -> Expl:
    vrf = n.fabric("mgmt_interface_settings.vrf")
    if vrf.where == NOT_SET:
        vrf = n.fabric("mgmt_interface_vrf", "MGMT")
    return Expl("The out-of-band management VRF. Default name MGMT (`mgmt_interface_settings.vrf`, formerly "
                "`mgmt_interface_vrf`); Management1, the default route to mgmt_gateway, eAPI, NTP and DNS all live in it. "
                "AVD renders `no ip routing vrf MGMT` (it is a host VRF).", [vrf], D("management-interface-settings"),
                related=[r"vrf MGMT", r"^ip route vrf MGMT"])


@rule("mgmt-intf", r"^interface (Management\S+)$", topic="management")
def _mgmtif(n: NodeCtx, m: M, cl, L) -> Expl:
    ip = n.setting("mgmt_ip")
    return Expl(f"Out-of-band management interface {m.group(1)} (platform `{n.setting('platform').value}` → "
                "`platform_settings[].management_interface`, Management1 for cEOSLab since AVD 6.0). Its address is the "
                f"node's `mgmt_ip` {ip.value}.", [ip, n.setting("platform")], D("node-type-common-configuration"),
                related=[r"^ip route vrf MGMT", r"vrf MGMT"])


@rule("mgmt-intf-ip", r"^ip address (\S+)$", section=r"^interface Management", topic="management")
def _mgmtip(n: NodeCtx, m: M, cl, L) -> Expl:
    ip = n.setting("mgmt_ip")
    knob = n.lab.knob_if("mgmt_subnet")
    ip.knob = knob
    extra = ""
    if knob and n.lab.template == "single-dc-l3ls":
        extra = (" The l3ls generator numbers mgmt IPs from the mgmt_subnet knob: spines .11+, L3 leafs .101+, "
                 "L2 leafs .151+ (offset + position).")
    return Expl(f"{m.group(1)} is the node's `mgmt_ip` (also the containerlab `mgmt-ipv4` and the Ansible/ANTA "
                f"`ansible_host`).{extra}", [ip], D("node-type-common-configuration"), related=[rf"{re.escape(ip_only(m.group(1)))}"])


@rule("mgmt-intf-vrf", r"^vrf (\S+)$", section=r"^interface Management", topic="management")
def _mgmtifvrf(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Management interface placed in VRF {m.group(1)} (`mgmt_interface_settings.vrf`, default MGMT).",
                [n.fabric("mgmt_interface_settings.vrf", "MGMT")], D("management-interface-settings"))


@rule("mgmt-intf-noshut", r"^no shutdown$", section=r"^interface Management", topic="management")
def _mgmtnoshut(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("The management interface is always enabled; its address is the node's `mgmt_ip`.", [n.setting("mgmt_ip")],
                D("node-type-common-configuration"))


@rule("mgmt-intf-desc", r"^description (OOB_MANAGEMENT|.*)$", section=r"^interface Management", topic="management")
def _mgmtdesc(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Description '{m.group(1)}' = `mgmt_interface_settings.description` (default OOB_MANAGEMENT).",
                [n.fabric("mgmt_interface_settings.description", "OOB_MANAGEMENT")], D("management-interface-settings"))


@rule("mgmt-route", r"^ip route vrf (\S+) 0\.0\.0\.0/0 (\S+)$", topic="management")
def _mgmtroute(n: NodeCtx, m: M, cl, L) -> Expl:
    gw = n.setting("mgmt_gateway")
    if gw.value is None:
        gw = n.fabric("mgmt_gateway")
    gw.knob = n.lab.knob_if("mgmt_gateway")
    return Expl(f"Default route of the management VRF {m.group(1)} via {m.group(2)} = `mgmt_gateway` (set once for the "
                "DC; a node-level `mgmt_gateway` would override it). Use `mgmt_destination_networks` for specific "
                "routes instead of a default.", [gw], D("management-interface-settings"))


@rule("no-ip-routing-mgmt", r"^no ip routing vrf (\S+)$", topic="management")
def _noiprouting(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"VRF {m.group(1)} is the management VRF: it only needs a default route, so AVD renders `no ip routing "
                "vrf` for it (`mgmt_interface_settings.vrf_routing: false`, the default).",
                [n.fabric("mgmt_interface_settings.vrf_routing", False)], D("management-interface-settings"))


@rule("eapi", r"^management api http-commands$", topic="management")
def _eapi(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("eAPI (HTTPS) — rendered because `management_eapi.enabled: true`. Since AVD 6.0 eAPI is NOT enabled by "
                "default; the lab enables it so Ansible (eos_config_deploy_eapi) and ANTA can talk to the switch. It is "
                "bound to the management VRF (default_mgmt_method oob).", [n.fabric("management_eapi")], D("management-eapi"),
                related=[r"^protocol https", r"^vrf MGMT$"])


@rule("eapi-child", r"^(protocol https|no shutdown|vrf \S+)$", section=r"^management api http-commands", topic="management")
def _eapichild(n: NodeCtx, m: M, cl, L) -> Expl:
    what = {"protocol https": "HTTPS transport (`management_eapi.enable_https: true`, default)",
            "no shutdown": "the API server is enabled"}.get(m.group(1), f"eAPI listens in {m.group(1)} (the management VRF)")
    return Expl(f"Part of the eAPI block from `management_eapi`: {what}.", [n.fabric("management_eapi")], D("management-eapi"))


@rule("ntp", r"^ntp (local-interface vrf (\S+) (\S+)|server vrf (\S+) (\S+).*)$", topic="management")
def _ntp(n: NodeCtx, m: M, cl, L) -> Expl:
    inp = n.fabric("ntp_settings")
    if m.group(1).startswith("local-interface"):
        why = (f"NTP is sourced from {m.group(3)} in VRF {m.group(2)}: `ntp_settings.server_vrf: use_mgmt_interface_vrf` "
               "resolves to the management VRF and interface.")
    else:
        prefer = " `prefer` is added to the first server (`set_first_ntp_server_as_preferred: true`, AVD 6.4 default)." if "prefer" in m.group(1) else ""
        why = f"NTP server {m.group(5)} from `ntp_settings.servers[name={m.group(5)}]` in VRF {m.group(4)}.{prefer}"
    return Expl(why + " CloudVision onboarding requires NTP servers.", [inp], D("time-configuration"), related=[r"^ntp "])


# ================================================================================================ spanning tree
@rule("stp-mode", r"^spanning-tree mode (\S+)$", topic="l2")
def _stpmode(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Spanning-tree mode {m.group(1)} = node setting `spanning_tree_mode`.", [n.setting("spanning_tree_mode")],
                D("node-type-l2-mlag-configuration"), related=[r"^spanning-tree"])


@rule("stp-prio", r"^spanning-tree mst 0 priority (\d+)$", topic="l2")
def _stpprio(n: NodeCtx, m: M, cl, L) -> Expl:
    p = n.setting("spanning_tree_priority", 32768)
    return Expl(f"MST instance 0 priority {m.group(1)} = `spanning_tree_priority`"
                + (" (not set → 32768 default)" if p.where == NOT_SET else "")
                + ". The L3 leafs use a low priority (4096) so the MLAG pair is the STP root for the L2 leafs below it.",
                [p], D("node-type-l2-mlag-configuration"))


@rule("stp-novlan", r"^no spanning-tree vlan-id (\S+)$", topic="mlag")
def _stpno(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"STP is disabled on VLANs {m.group(1)}: the MLAG peer VLAN (`mlag_peer_vlan`, 4094) and the L3 peering "
                "VLAN (`mlag_peer_l3_vlan`, 4093) only exist on the peer-link and must never be blocked.",
                [n.setting("mlag_peer_vlan", 4094), n.setting("mlag_peer_l3_vlan", 4093)], D("node-type-l2-mlag-configuration"),
                related=[r"^vlan 409[34]$"])


# ================================================================================================ VLANs / VRFs
def _vlan_expl(n: NodeCtx, vid: int, what: str) -> Expl | None:
    mlag_vlan = _int(n.setting("mlag_peer_vlan", 4094).value, 4094)
    mlag_l3 = _int(n.setting("mlag_peer_l3_vlan", 4093).value, 4093)
    if vid == mlag_vlan:
        return Expl(f"{what} {vid} is the MLAG peer VLAN (`mlag_peer_vlan`, default 4094): carries the MLAG control "
                    "session between the two peers, only allowed on the peer-link (trunk group MLAG).",
                    [n.setting("mlag_peer_vlan", 4094)], D("node-type-l2-mlag-configuration"),
                    related=[rf"Vlan{vid}\b", rf"^vlan {vid}$", r"^local-interface"], topic="mlag")
    if vid == mlag_l3:
        return Expl(f"{what} {vid} is the MLAG L3 peering VLAN (`mlag_peer_l3_vlan`, default 4093): the underlay iBGP "
                    "session between the MLAG peers runs over it (peer group MLAG-IPv4-UNDERLAY-PEER).",
                    [n.setting("mlag_peer_l3_vlan", 4093)], D("node-type-l2-mlag-configuration"),
                    related=[rf"Vlan{vid}\b", rf"^vlan {vid}$"], topic="mlag")
    inband = _inband(n, vid)
    if inband:
        return inband
    found = n.find_vlan(vid)
    if found:
        key, t, vrf, obj, kind = found
        where = n.where_of(key)
        if kind == "svi":
            return Expl(f"{what} {vid} ('{obj.get('name')}') is SVI id {vid} of VRF {vrf['name']} in tenant {t['name']}: "
                        f"`{key}[name={t['name']}].vrfs[name={vrf['name']}].svis[id={vid}]`. The VLAN is created on every "
                        "node whose filter matches the tenant/tags (and on the L2 leafs below them).",
                        [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}].svis[id={vid}]", obj, where, n.lab.knob_if("tenants"))],
                        D("network-services-vrfs-svis-settings"), related=[rf"\bvlan {vid}\b", rf"Vlan{vid}\b", rf"vxlan vlan {vid} "],
                        topic="services")
        return Expl(f"{what} {vid} ('{obj.get('name')}') is a pure L2 VLAN of tenant {t['name']}: "
                    f"`{key}[name={t['name']}].l2vlans[id={vid}]` — no SVI, but it is still stretched with VXLAN (L2VNI).",
                    [Input(f"{key}[name={t['name']}].l2vlans[id={vid}]", obj, where, n.lab.knob_if("tenants"))],
                    D("network-services-l2vlans-settings"), related=[rf"\bvlan {vid}\b", rf"vxlan vlan {vid} "], topic="services")
    base = _int(n.fabric("mlag_ibgp_peering_vrfs.base_vlan", 3000).value, 3000)
    for key, t in n.tenants():
        for vrf in t.get("vrfs") or []:
            vrf_id = _int(vrf.get("vrf_id", vrf.get("vrf_vni")))
            peering = _int(vrf.get("mlag_ibgp_peering_vlan"), base + vrf_id - 1)
            if peering == vid:
                return Expl(f"{what} {vid} is the per-VRF MLAG iBGP peering VLAN of {vrf['name']}: "
                            f"`mlag_ibgp_peering_vrfs.base_vlan` ({base}) + vrf_id ({vrf_id}) - 1 = {vid}. The two MLAG "
                            "peers run an iBGP session inside the VRF over it so each can reach hosts behind the other "
                            "(`enable_mlag_ibgp_peering_vrfs`, default true).",
                            [Input("mlag_ibgp_peering_vrfs.base_vlan", base, n.where_of("mlag_ibgp_peering_vrfs")),
                             Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}].vrf_id|vrf_vni", vrf_id, n.where_of(key))],
                            D("network-services-vrfs-settings"), related=[rf"Vlan{vid}\b", rf"^vlan {vid}$"], topic="mlag")
    return None


def _inband(n: NodeCtx, vid: int) -> Expl | None:
    """Inband management VLAN/SVI (L2 switches managed through their uplinks; parents get the SVI + VARP gateway)."""
    own = n.setting("inband_mgmt_vlan", 4092)
    subnet = n.setting("inband_mgmt_subnet")
    children = [e for e in n.sc.get("vlan_interfaces") or [] if e.get("name") == f"Vlan{vid}"
                and "Inband" in str(e.get("description") or "")]
    vlan_named = any(v.get("id") == vid and "INBAND" in str(v.get("name") or "") for v in n.sc.get("vlans") or [])
    if not (children or vlan_named or (subnet.value and _int(own.value, 4092) == vid)):
        return None
    return Expl(f"VLAN {vid} is the inband management VLAN (`inband_mgmt_vlan`, default 4092; name INBAND_MGMT). Set on "
                "L2 switches with `inband_mgmt_subnet`: AVD creates the VLAN + SVI on the switch itself and on all its "
                "uplink switches, where the SVI gets per-node IPs (L3 parents +2/+3) and a VARP gateway (subnet +1), so the "
                "L2 switches are managed through their uplinks instead of an OOB port.",
                [own, subnet], D("node-type-inband-management-configuration"), related=[rf"Vlan{vid}\b", rf"^vlan {vid}$"],
                topic="management")


@rule("vlan", r"^vlan (\d+)$", topic="services")
def _vlan(n: NodeCtx, m: M, cl, L) -> Expl | None:
    return _vlan_expl(n, int(m.group(1)), "VLAN")


@rule("vlan-name", r"^name (\S+)$", section=r"^vlan \d+$", topic="services")
def _vlanname(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    vid = int(L.header(cl).split()[1])
    ex = _vlan_expl(n, vid, "VLAN")
    if ex:
        ex.why = f"VLAN name '{m.group(1)}': " + ex.why
    return ex


@rule("vlan-trunk-group", r"^trunk group (\S+)$", section=r"^vlan \d+$", topic="mlag")
def _vlantg(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Trunk group {m.group(1)} restricts this VLAN to the MLAG peer-link (the peer-link Port-Channel is in "
                "trunk group MLAG). Name from `trunk_groups.mlag.name` / `trunk_groups.mlag_l3.name` (default MLAG).",
                [n.fabric("trunk_groups", {"mlag": {"name": "MLAG"}})], D("fabric-settings"),
                related=[rf"trunk group {m.group(1)}"])


@rule("vrf-instance", r"^vrf instance (\S+)$", topic="services")
def _vrf(n: NodeCtx, m: M, cl, L) -> Expl | None:
    f = n.find_vrf(m.group(1))
    if not f:
        return None
    key, t, vrf = f
    return Expl(f"VRF {vrf['name']} of tenant {t['name']} (`{key}[name={t['name']}].vrfs[name={vrf['name']}]`). A leaf "
                "renders the VRF because at least one of its SVIs matches the node's filter; it gets `ip routing vrf`, "
                "an L3VNI on Vxlan1 and a BGP VRF with RD/RT.",
                [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}]", {k: v for k, v in vrf.items() if k != "svis"},
                       n.where_of(key), n.lab.knob_if("tenants"))],
                D("network-services-vrfs-settings"), related=[rf"vrf {re.escape(vrf['name'])}\b"])


@rule("ip-routing", r"^ip routing$", topic="routing")
def _iprouting(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"`ip routing` because node type `{n.type_key}` is an underlay router (`node_type_keys[].underlay_router: "
                "true`; L2 types like l2leaf have it false and only route in the management VRF).",
                [Input(f"node_type_keys[key={n.type_key}].underlay_router", True, NOT_SET)], D("node-type-keys"))


@rule("ip-routing-vrf", r"^ip routing vrf (\S+)$", topic="services")
def _iproutingvrf(n: NodeCtx, m: M, cl, L) -> Expl | None:
    f = n.find_vrf(m.group(1))
    if not f:
        return None
    key, t, vrf = f
    return Expl(f"Routing enabled in tenant VRF {vrf['name']} (L3 network services on `{n.type_key}`).",
                [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}]", vrf.get("name"), n.where_of(key))],
                D("network-services-vrfs-settings"))


# ================================================================================================ loopbacks
def _lo0(n: NodeCtx) -> tuple[Expl, str | None]:
    pool, nid, off = n.setting("loopback_ipv4_pool"), n.setting("id"), n.setting("loopback_ipv4_offset", 0)
    pool.knob = n.lab.knob_if("pools")
    idx = _int(nid.value) + _int(off.value)
    ip = pool_ip(pool.value, 32, idx, 0) if pool.value else None
    return Expl(f"Loopback0 {ip} = loopback_ipv4_pool {pool.value} + id {nid.value} + loopback_ipv4_offset {off.value} "
                f"(address #{idx} of the pool). It is the router-id and the EVPN peering address; the offset keeps the "
                "leafs clear of the spines that share the pool.", [pool, nid, off], D("node-type-loopback-vtep-configuration"),
                related=[rf"\b{re.escape(ip or 'x')}\b", r"^router-id"], topic="addressing"), ip


@rule("loopback0", r"^interface Loopback0$", topic="addressing")
def _loopback0(n: NodeCtx, m: M, cl, L) -> Expl:
    return _lo0(n)[0]


def _vtep(n: NodeCtx) -> tuple[Expl, str | None]:
    pool, nid, off = n.setting("vtep_loopback_ipv4_pool"), n.setting("id"), n.setting("loopback_ipv4_offset", 0)
    pool.knob = n.lab.knob_if("pools")
    primary, pid = n.mlag_primary()
    if primary:
        idx = _int(pid) + _int(off.value)
        ip = pool_ip(pool.value, 32, idx, 0) if pool.value else None
        why = (f"Loopback1 (VTEP) {ip} = vtep_loopback_ipv4_pool {pool.value} + MLAG primary id {pid} ({primary}) + "
               f"loopback_ipv4_offset {off.value}. Both MLAG peers share the same VTEP IP, so remote VTEPs see the pair "
               "as one logical VTEP (anycast VTEP).")
    else:
        idx = _int(nid.value) + _int(off.value)
        ip = pool_ip(pool.value, 32, idx, 0) if pool.value else None
        why = f"Loopback1 (VTEP) {ip} = vtep_loopback_ipv4_pool {pool.value} + id {nid.value} + loopback_ipv4_offset {off.value}."
    return Expl(why + " It is the VXLAN source interface.", [pool, nid, off], D("node-type-loopback-vtep-configuration"),
                related=[r"vxlan source-interface", rf"\b{re.escape(ip or 'x')}\b"], topic="addressing"), ip


@rule("loopback1", r"^interface (Loopback1)$", topic="addressing")
def _loopback1(n: NodeCtx, m: M, cl, L) -> Expl | None:
    vtep = n.setting("vtep_loopback", "Loopback1").value or "Loopback1"
    return _vtep(n)[0] if vtep == m.group(1) and n.setting("vtep_loopback_ipv4_pool").value else None


def _diag(n: NodeCtx, num: int) -> Expl | None:
    for key, t in n.tenants():
        for vrf in t.get("vrfs") or []:
            vd = vrf.get("vtep_diagnostic") or {}
            if _int(vd.get("loopback"), -1) == num:
                nid, off = n.setting("id"), n.setting("loopback_ipv4_offset", 0)
                rng = vd.get("loopback_ip_range")
                idx = _int(nid.value) + _int(off.value)
                ip = pool_ip(rng, 32, idx, 0) if rng else None
                return Expl(f"Loopback{num} is the VTEP diagnostic loopback of VRF {vrf['name']}: {ip} = "
                            f"vtep_diagnostic.loopback_ip_range {rng} + id {nid.value} + loopback_ipv4_offset {off.value}. "
                            "It gives every leaf a unique IP inside the VRF (ping/traceroute source) and backs "
                            "`ip address virtual source-nat`.",
                            [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}].vtep_diagnostic", vd, n.where_of(key)), nid, off],
                            D("network-services-vrfs-settings"), related=[rf"source-nat vrf {vrf['name']}\b", rf"DIAG_VRF_{vrf['name']}"],
                            topic="services")
    return None


@rule("loopback-diag", r"^interface Loopback(\d+)$", topic="services")
def _loopbackn(n: NodeCtx, m: M, cl, L) -> Expl | None:
    return _diag(n, int(m.group(1)))


@rule("loopback-child", r"^(ip address \S+|description \S+|no shutdown|vrf \S+)$", section=r"^interface Loopback\d+$", topic="addressing")
def _lochild(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    num = int(L.header(cl).replace("interface Loopback", ""))
    if num == 0:
        ex = _lo0(n)[0]
        desc = "`router_id_loopback_description` (default ROUTER_ID)"
    elif num == 1 and n.setting("vtep_loopback_ipv4_pool").value:
        ex = _vtep(n)[0]
        desc = "`vtep_loopback_description` (default VXLAN_TUNNEL_SOURCE)"
    else:
        ex = _diag(n, num)
        desc = "`default_vrf_diag_loopback_description` (default DIAG_VRF_{vrf})"
    if ex is None:
        return None
    if m.group(1).startswith("description"):
        ex.why = f"Description from {desc}. " + ex.why
    return ex


@rule("source-nat", r"^ip address virtual source-nat vrf (\S+) address (\S+)$", topic="services")
def _snat(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    for key, t in n.tenants():
        for vrf in t.get("vrfs") or []:
            if vrf.get("name") == m.group(1) and (vrf.get("vtep_diagnostic") or {}).get("loopback") is not None:
                ex = _diag(n, _int(vrf["vtep_diagnostic"]["loopback"]))
                if ex:
                    ex.why = (f"Traffic the switch itself originates in VRF {m.group(1)} (e.g. ping to a host) is sourced from "
                              f"{m.group(2)} instead of the shared anycast SVI address. " + ex.why)
                return ex
    return None


# ================================================================================================ P2P uplinks / fabric links
def _p2p(n: NodeCtx, iface: str) -> Expl | None:
    """Explain the /31 of a routed fabric link on either side (child = the node with uplink_switches)."""
    eth = n.ethernet(iface) or {}
    meta = eth.get("metadata") or {}
    peer = meta.get("peer")
    if not peer or not eth.get("ip_address"):
        return None
    uplinks = n.setting("uplink_switches").value or []
    if peer in uplinks:  # this node is the child
        child, parent, child_if = n, peer, iface
    else:  # this node is the parent: explain from the child's inputs
        from .explain import NodeCtx as _N

        child = _N(n.lab, peer)
        parent, child_if = n.host, meta.get("peer_interface")
        if n.host not in (child.setting("uplink_switches").value or []):
            return None
    ups = list(child.setting("uplink_switches").value or [])
    child_eth = child.ethernet(child_if) or {}
    # index of this uplink: position of the child's interface among its uplinks to the parents, in uplink_switches order
    child_uplinks = [e for e in child.sc.get("ethernet_interfaces") or []
                     if (e.get("metadata") or {}).get("peer") in ups and e.get("ip_address")]
    try:
        i = [e["name"] for e in child_uplinks].index(child_if)
    except ValueError:
        i = ups.index(parent) if parent in ups else 0
    pool = child.setting("uplink_ipv4_pool")
    pool.knob = n.lab.knob_if("pools")
    cid = child.setting("id")
    mus = child.setting("max_uplink_switches", len(ups))
    mpu = child.setting("max_parallel_uplinks", 1)
    max_up = _int(mus.value, len(ups)) or len(ups)
    par = _int(mpu.value, 1) or 1
    off = (_int(cid.value) - 1) * max_up * par + i
    subnet = pool_ip(pool.value, 31, off, 0) if pool.value else None
    child_ip, parent_ip = (pool_ip(pool.value, 31, off, 1), subnet) if pool.value else (None, None)
    side = "child (downstream) side = subnet + 1" if n.host == child.host else "parent (uplink switch) side = subnet + 0"
    return Expl(f"P2P link {child.host} {child_if} ↔ {parent} ({(child_eth.get('metadata') or {}).get('peer_interface') or '?'}): "
                f"/31 #{off} of uplink_ipv4_pool {pool.value} where #{off} = (id {cid.value} - 1) × max_uplink_switches "
                f"{max_up} × max_parallel_uplinks {par} + uplink index {i}. Subnet {subnet}/31 → {child.host} {child_ip}, "
                f"{parent} {parent_ip}; this is the {side}. Links are always defined from the child's perspective "
                f"(`uplink_switches` on {child.host}); raising max_uplink_switches reserves room for more spines but "
                "renumbers every link.",
                [pool, cid, child.setting("uplink_switches"), Input(mus.key, max_up, mus.where), Input(mpu.key, par, mpu.where)],
                D("node-type-uplink-configuration"),
                related=[rf"\b{re.escape(str(child_ip))}\b", rf"\b{re.escape(str(parent_ip))}\b"], topic="underlay")


def _eth_kind(n: NodeCtx, iface: str) -> tuple[str, dict[str, Any]]:
    eth = n.ethernet(iface) or n.port_channel(iface) or {}
    meta = eth.get("metadata") or {}
    ptype = str(meta.get("peer_type") or "")
    desc = str(eth.get("description") or "")
    if ptype == "mlag_peer" or desc.startswith("MLAG_"):
        return "mlag", eth
    if meta.get("peer_key") or n.endpoint_adapter(iface):
        return "endpoint", eth
    if eth.get("ip_address") and meta.get("peer"):
        return "p2p", eth
    if eth.get("channel_group") or desc.startswith("L2_") or iface.startswith("Port-Channel"):
        return "l2link", eth
    return "other", eth


def _mlag_po_id(n: NodeCtx) -> tuple[int | None, Input]:
    inp = n.setting("mlag_interfaces")
    pcid = n.setting("mlag_port_channel_id")
    if pcid.value is not None:
        return _int(pcid.value), pcid
    mi = (n.sc.get("mlag_configuration") or {}).get("peer_link") or ""
    return (_int(mi.replace("Port-Channel", "")) if mi else None), inp


def _endpoint_expl(n: NodeCtx, iface: str, is_po: bool) -> Expl | None:
    if is_po:
        n.port_channel(iface) or {}
        members = [e["name"] for e in n.sc.get("ethernet_interfaces") or []
                   if (e.get("channel_group") or {}).get("id") == _int(iface.replace("Port-Channel", ""))]
        found = n.endpoint_adapter(members[0]) if members else None
    else:
        found = n.endpoint_adapter(iface)
    if not found:
        return None
    key, ep, ad, idx = found
    where = n.endpoint_where(key)
    port = ad["switch_ports"][idx]
    ep_port = (ad.get("endpoint_ports") or [None] * (idx + 1))[idx] if ad.get("endpoint_ports") else None
    mode = ad.get("mode", "EOS default")
    pc = ad.get("port_channel") or {}
    dual = len(set(ad.get("switches") or [])) == 2
    txt = (f"Connected endpoint '{ep['name']}' ({key}): adapter switches {ad.get('switches')} ports {ad.get('switch_ports')}"
           f"{' endpoint port ' + str(ep_port) if ep_port else ''}; mode {mode}, vlans {ad.get('vlans', 'EOS default')}.")
    if pc.get("mode"):
        cid = pc.get("channel_id") or _int(re.sub(r"\D", "", str(ad["switch_ports"][0])))
        txt += (f" `port_channel.mode: {pc['mode']}` bundles the ports into Port-Channel{cid} (channel_id defaults to "
                f"the number of the first switch port, {ad['switch_ports'][0]})")
        if dual:
            txt += f"; the two switches are an MLAG pair, so the Port-Channel gets `mlag {cid}` (one LAG across both switches)"
        txt += "."
    txt += " Description template `<TYPE>_<endpoint>_<endpoint_port>` (default_connected_endpoints_description)."
    return Expl(txt, [Input(f"{key}[name={ep['name']}].adapters[{(ep.get('adapters') or []).index(ad)}]", ad, where,
                            n.lab.knob_if(key))], D("connected-endpoints"),
                related=[re.escape(ep["name"]), rf"^interface {re.escape(port)}$"], topic="endpoints")


def _mlag_member(n: NodeCtx, iface: str) -> Expl:
    pid, inp = _mlag_po_id(n)
    peer = n.mlag_peer()
    return Expl(f"{iface} is an MLAG peer-link member towards {peer} (`mlag_interfaces`, here from "
                f"`default_interfaces[types: {n.type_key}].mlag_interfaces` unless set on the node). All members join "
                f"Port-Channel{pid} (`mlag_port_channel_id`, default = number of the first mlag interface).",
                [n.setting("mlag_interfaces"), Input("default_interfaces", n.fabric("default_interfaces").value, n.where_of("default_interfaces"))],
                D("node-type-l2-mlag-configuration"), related=[rf"Port-Channel{pid}\b", r"peer-link"], topic="mlag")


def _l2link(n: NodeCtx, iface: str, eth: dict[str, Any]) -> Expl:
    meta = eth.get("metadata") or {}
    peer = meta.get("peer") or str(eth.get("description") or "").split("_")[1:2]
    ups = n.setting("uplink_switches").value or []
    if any(str(u) in str(eth.get("description") or "") for u in ups) or (meta.get("peer") in ups):
        return Expl(f"{iface} is an L2 uplink of this {n.type_key} (`uplink_type: port-channel`, the default for "
                    f"`{n.type_key}`): uplinks to {ups} are bundled into one Port-Channel; because the parents are an "
                    "MLAG pair it is a single LAG to both. Allowed VLANs = the VLANs this switch renders.",
                    [n.setting("uplink_switches"), n.setting("uplink_interfaces")], D("node-type-uplink-configuration"),
                    related=[r"^interface Port-Channel", r"channel-group"], topic="l2")
    return Expl(f"{iface} is a downlink to an L2 switch ({peer}). Links are defined on the child: the L2 leaf lists this "
                "node in `uplink_switches`, and this side's port comes from `default_interfaces[].downlink_interfaces` "
                "indexed by the child's id. The L3 leaf pair presents one MLAG Port-Channel to it.",
                [Input("default_interfaces", n.fabric("default_interfaces").value, n.where_of("default_interfaces"))],
                D("node-type-uplink-configuration"), related=[r"^interface Port-Channel", r"channel-group"], topic="l2")


def _core_link(n: NodeCtx, iface: str) -> Expl | None:
    """core_interfaces / l3_edge p2p_links: explicit point-to-point links (nodes + interfaces + ip lists)."""
    for key in ("core_interfaces", "l3_edge"):
        block = n.hv.get(key) or {}
        for idx, link in enumerate(block.get("p2p_links") or []):
            nodes, ifs = link.get("nodes") or [], link.get("interfaces") or []
            for i, (node, ifname) in enumerate(zip(nodes, ifs)):
                if node == n.host and ifname == iface:
                    peer = nodes[1 - i] if len(nodes) == 2 else "?"
                    ip = (link.get("ip") or [None, None])[i] if link.get("ip") else None
                    via = "an IP pool (`ip_pool` / `p2p_links_ip_pools`)" if not ip else f"the explicit `ip` list ({ip})"
                    proto = " It is included in the underlay routing protocol (`include_in_underlay_protocol: true`)." \
                        if link.get("include_in_underlay_protocol", True) else ""
                    return Expl(f"{iface} is a {key} point-to-point link {n.host} ↔ {peer} ({key}.p2p_links[{idx}]): "
                                f"`nodes`, `interfaces` and `ip` are two-item lists read in the same order; the address "
                                f"comes from {via}.{proto} Used for links AVD cannot derive from uplink_switches "
                                "(WAN/DCI/core meshes).",
                                [Input(f"{key}.p2p_links[{idx}]", link, n.where_of(key))],
                                D("core-interfaces" if key == "core_interfaces" else "l3-edge"), topic="underlay")
    return None


@lru_cache(maxsize=512)
def _expand(ports: tuple[str, ...]) -> frozenset[str]:
    from pyavd.j2filters import range_expand

    return frozenset(range_expand(list(ports)))


def _l3_interface(n: NodeCtx, iface: str) -> Expl | None:
    """Routed (sub)interfaces: tenant `vrfs[].l3_interfaces` (index-aligned nodes/interfaces/ip_addresses) or the node's
    own `l3_interfaces`."""
    for key, t in n.tenants():
        for vrf in t.get("vrfs") or []:
            for idx, l3 in enumerate(vrf.get("l3_interfaces") or []):
                for i, (node, ifname) in enumerate(zip(l3.get("nodes") or [], l3.get("interfaces") or [])):
                    if node == n.host and ifname == iface:
                        ip = (l3.get("ip_addresses") or [None] * (i + 1))[i] if l3.get("ip_addresses") else None
                        return Expl(f"{iface} is a routed interface of VRF {vrf['name']} (tenant {t['name']}): "
                                    f"`vrfs[name={vrf['name']}].l3_interfaces[{idx}]` — nodes, interfaces and ip_addresses "
                                    f"are index-aligned lists (this node → {ip}). A dotted name is a subinterface "
                                    "(`encapsulation dot1q vlan`).",
                                    [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}].l3_interfaces[{idx}]", l3,
                                           n.where_of(key))], D("network-services-vrfs-l3-interfaces-settings"), topic="services")
    own = n.setting("l3_interfaces")
    for idx, l3 in enumerate(own.value or []):
        if l3.get("name") == iface:
            return Expl(f"{iface} is one of the node's own `l3_interfaces` (routed port, e.g. a WAN or edge link): "
                        f"ip {l3.get('ip_address')}, peer {l3.get('peer')}.", [own], D("node-type-l3-interfaces-configuration"),
                        topic="underlay")
    return None


def _network_port(n: NodeCtx, iface: str) -> Expl | None:
    """network_ports: port-centric bulk model — switches (regex fullmatch) × expanded switch_ports ranges."""
    for idx, np in enumerate(n.hv.get("network_ports") or []):
        if not any(re.fullmatch(str(rx), n.host) for rx in np.get("switches") or []):
            continue
        if n.setting("platform").value and np.get("platforms") and not any(
                re.fullmatch(str(rx), str(n.setting("platform").value)) for rx in np["platforms"]):
            continue
        if iface in _expand(tuple(np.get("switch_ports") or [])):
            prof = f" Profile `{np['profile']}` (port_profiles) supplies the rest; adapter keys win over the profile." if np.get("profile") else ""
            return Expl(f"{iface} matches network_ports[{idx}] (switches {np.get('switches')} as full-match regexes, "
                        f"switch_ports {np.get('switch_ports')} expanded to single ports): mode {np.get('mode', 'from profile')}, "
                        f"description '{np.get('description', '')}'.{prof} network_ports configure whole port ranges at once "
                        "— typical for campus access switches.",
                        [Input(f"network_ports[{idx}]", np, n.where_of("network_ports"))], D("network-ports"),
                        topic="endpoints")
    return None


def _iface_expl(n: NodeCtx, iface: str) -> Expl | None:
    kind, eth = _eth_kind(n, iface)
    if kind == "p2p":
        ex = _p2p(n, iface)
        if ex:
            return ex
    ex = _core_link(n, iface)
    if ex:
        return ex
    if kind == "mlag":
        return _mlag_member(n, iface)
    if kind == "endpoint":
        ex = _endpoint_expl(n, iface, iface.startswith("Port-Channel"))
        if ex:
            return ex
    ex = _network_port(n, iface) or _l3_interface(n, iface.split(" ")[0])
    if ex:
        return ex
    if kind == "l2link":
        return _l2link(n, iface, eth)
    return None


@rule("ethernet", r"^interface (Ethernet\S+)$", topic="interfaces")
def _ethernet(n: NodeCtx, m: M, cl, L) -> Expl | None:
    return _iface_expl(n, m.group(1))


@rule("port-channel", r"^interface (Port-Channel\d+)$", topic="interfaces")
def _po(n: NodeCtx, m: M, cl, L) -> Expl | None:
    iface = m.group(1)
    pid, inp = _mlag_po_id(n)
    if pid is not None and iface == f"Port-Channel{pid}" and n.mlag_peer():
        return Expl(f"{iface} is the MLAG peer-link to {n.mlag_peer()}: formed automatically because {n.host} and "
                    f"{n.mlag_peer()} are the two nodes of node_group {n.group_name} (`mlag_support: true` for "
                    f"`{n.type_key}`, `mlag: true` default). Members = `mlag_interfaces`, id = number of the first member. "
                    "Trunk group MLAG keeps VLANs 4093/4094 on this link only.",
                    [Input(f"{n.type_key}.node_groups[group={n.group_name}]", [x.get('name') for x in n.node_group.get('nodes', [])],
                           n.setting("id").where), inp], D("node-type-l2-mlag-configuration"),
                    related=[r"peer-link", rf"channel-group {pid} "], topic="mlag")
    kind, eth = _eth_kind(n, iface)
    if kind == "endpoint":
        return _endpoint_expl(n, iface, True)
    ex = _endpoint_expl(n, iface, True)
    if ex:
        return ex
    return _l2link(n, iface, eth) if eth else None


# ---- interface children
@rule("intf-desc", r"^description (\S+)$", section=r"^interface (Ethernet|Port-Channel)", topic="interfaces")
def _intfdesc(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    d = m.group(1)
    table = {"P2P_": ("default_underlay_p2p_ethernet_description", "P2P_{peer}_{peer_interface}"),
             "MLAG_": ("mlag_member_description / mlag_port_channel_description", "MLAG_{mlag_peer}_{peer_interface}"),
             "L2_": ("underlay_l2_ethernet_description / underlay_l2_port_channel_description", "L2_{peer}_{peer_interface}")}
    for pfx, (key, tpl) in table.items():
        if d.startswith(pfx):
            return Expl(f"Description '{d}' is generated from the template `{tpl}` (`{key}`, AVD string-formatter "
                        "syntax). Change the template fabric-wide rather than per interface.",
                        [Input(key.split(" ")[0], tpl, n.where_of(key.split(" ")[0]))], D("fabric-settings"), topic="interfaces")
    iface = L.header(cl).split()[1]
    ex = _endpoint_expl(n, iface, iface.startswith("Port-Channel"))
    if ex:
        ex.why = f"Description '{d}': " + ex.why
    return ex


@rule("intf-ip", r"^ip address (\S+)$", section=r"^interface Ethernet", topic="underlay")
def _intfip(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    return _p2p(n, L.header(cl).split()[1])


@rule("intf-mtu", r"^mtu (\d+)$", section=r"^interface (Ethernet|Vlan)", topic="underlay")
def _intfmtu(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    hdr = L.header(cl)
    if hdr.startswith("interface Vlan"):
        return Expl(f"MTU {m.group(1)} on the MLAG peering SVI follows `p2p_uplinks_mtu` (default 9214) so the underlay "
                    "path over the peer-link matches the fabric links.", [n.fabric("p2p_uplinks_mtu", 9214)], D("fabric-settings"))
    return Expl(f"MTU {m.group(1)} on a P2P fabric link: precedence node `uplink_mtu` → platform_settings[].p2p_uplinks_mtu "
                "→ `p2p_uplinks_mtu` → 9214. VXLAN adds 50 bytes, so the underlay needs a bigger MTU than the hosts.",
                [n.setting("uplink_mtu"), n.fabric("p2p_uplinks_mtu", 9214)], D("fabric-settings"))


@rule("intf-noswitchport", r"^no switchport$", section=r"^interface Ethernet", topic="underlay")
def _noswp(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    ex = _p2p(n, L.header(cl).split()[1])
    if ex:
        ex.why = "Routed port (`uplink_type: p2p`): L3 fabric links carry no VLANs. " + ex.why
    return ex


@rule("intf-channel-group", r"^channel-group (\d+) mode (\S+)$", section=r"^interface Ethernet", topic="interfaces")
def _chg(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    iface = L.header(cl).split()[1]
    kind, eth = _eth_kind(n, iface)
    if kind == "mlag":
        return _mlag_member(n, iface)
    if kind == "endpoint":
        return _endpoint_expl(n, iface, False)
    return _l2link(n, iface, eth)


@rule("po-mlag-id", r"^mlag (\d+)$", section=r"^interface Port-Channel", topic="mlag")
def _pomlag(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    iface = L.header(cl).split()[1]
    base = _endpoint_expl(n, iface, True)
    why = (f"`mlag {m.group(1)}` joins this Port-Channel with the same-numbered Port-Channel on the MLAG peer "
           f"{n.mlag_peer()} into one logical LAG (the device on the other end sees one LACP partner).")
    if base:
        base.why = why + " " + base.why
        return base
    return Expl(why + " Rendered for downlinks to dual-homed L2 switches.", [n.setting("mlag_interfaces")],
                D("node-type-l2-mlag-configuration"), topic="mlag")


@rule("po-switchport", r"^(switchport( mode \S+| trunk group \S+| trunk allowed vlan \S+| trunk native vlan \d+| access vlan \d+)?|spanning-tree portfast)$",
      section=r"^interface (Port-Channel|Ethernet)", topic="interfaces")
def _poswp(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    iface = L.header(cl).split()[1]
    pid, _inp = _mlag_po_id(n)
    if iface == f"Port-Channel{pid}":
        return Expl(f"'{m.group(1)}' on the MLAG peer-link: a trunk carrying all VLANs, plus trunk group MLAG for the "
                    "peer VLANs.", [n.setting("mlag_peer_link_allowed_vlans")], D("node-type-l2-mlag-configuration"), topic="mlag")
    ex = _endpoint_expl(n, iface, iface.startswith("Port-Channel"))
    line = m.group(1)
    if ex:
        pre = {"spanning-tree portfast": "`spanning_tree_portfast: edge` on the adapter → portfast (edge port). ",
               }.get(line, "")
        if "allowed vlan" in line:
            pre = "Allowed VLANs = the adapter's `vlans`. "
        elif "native vlan" in line:
            pre = "Native VLAN = the adapter's `native_vlan`. "
        elif "access vlan" in line:
            pre = "Access VLAN = the adapter's `vlans` with `mode: access`. "
        ex.why = pre + ex.why
        return ex
    kind, eth = _eth_kind(n, iface)
    if kind == "l2link" and "allowed vlan" in line:
        return Expl(f"{line}: AVD computes the VLANs of an L2 link from the VLANs the downstream switch renders (its "
                    "tenants/tags filter and endpoints) — no manual VLAN lists on fabric trunks.",
                    [n.setting("filter")], D("node-type-evpn-services-configuration"), topic="l2")
    if kind == "l2link":
        return _l2link(n, iface, eth)
    return None


@rule("intf-no-shutdown", r"^no shutdown$", section=r"^interface (Ethernet|Port-Channel|Vlan)", topic="interfaces")
def _noshut(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    hdr = L.header(cl)
    if hdr.startswith("interface Vlan"):
        vid = int(hdr.replace("interface Vlan", ""))
        found = n.find_vlan(vid)
        svi = found[3] if found else {}
        return Expl("SVI admin state: AVD renders `shutdown` unless the SVI sets `enabled: true` — a classic "
                    f"pitfall. {'This SVI has enabled: ' + str(svi.get('enabled')) if found else 'MLAG/peering SVIs are always up.'}",
                    [Input("svis[].enabled", svi.get("enabled"), n.where_of(found[0]) if found else NOT_SET)],
                    D("network-services-vrfs-svis-settings"))
    return Expl("Interfaces AVD manages are explicitly enabled (`shutdown: false`). Interfaces towards peers marked "
                "`is_deployed: false` would be shut (`shutdown_interfaces_towards_undeployed_peers`).",
                [n.fabric("shutdown_interfaces_towards_undeployed_peers", True)], D("fabric-settings"))


# ================================================================================================ SVIs / VARP
@rule("svi", r"^interface Vlan(\d+)$", topic="services")
def _svi(n: NodeCtx, m: M, cl, L) -> Expl | None:
    return _svi_expl(n, int(m.group(1)), None)


def _mlag_ip(n: NodeCtx, pool_key: str, what: str) -> tuple[str | None, list[Input], str]:
    pool = n.setting(pool_key)
    pool.knob = n.lab.knob_if("pools")
    primary, pid = n.mlag_primary()
    algo = n.fabric("fabric_ip_addressing.mlag.algorithm", "first_id")
    if not pool.value or pid is None:
        return None, [pool], ""
    offset = _int(pid) - 1 if algo.value in (None, "first_id") else 0
    is_primary = primary == n.host
    ip = pool_ip(pool.value, 31, offset, 0 if is_primary else 1)
    peer_ip = pool_ip(pool.value, 31, offset, 1 if is_primary else 0)
    why = (f"{what} {ip}/31 = /31 #{offset} of {pool_key} {pool.value} (algorithm {algo.value or 'first_id'}: subnet = "
           f"MLAG primary id {pid} - 1); the primary ({primary}, first node of the group) takes +0, the secondary +1. "
           f"Peer address {peer_ip}.")
    return ip, [pool, n.setting("id"), algo], why


def _svi_expl(n: NodeCtx, vid: int, sub: str | None) -> Expl | None:
    inband = _inband(n, vid)
    if inband:
        return inband
    mlag_vlan = _int(n.setting("mlag_peer_vlan", 4094).value, 4094)
    mlag_l3 = _int(n.setting("mlag_peer_l3_vlan", 4093).value, 4093)
    if vid == mlag_vlan:
        ip, inputs, why = _mlag_ip(n, "mlag_peer_ipv4_pool", f"Vlan{vid} (MLAG peer SVI)")
        return Expl(why + " `local-interface` of `mlag configuration`; `no autostate` keeps it up regardless of ports.",
                    inputs, D("node-type-l2-mlag-configuration"), related=[r"^peer-address", r"^local-interface"], topic="mlag")
    if vid == mlag_l3:
        ip, inputs, why = _mlag_ip(n, "mlag_peer_l3_ipv4_pool", f"Vlan{vid} (MLAG L3 peering SVI)")
        return Expl(why + " The underlay iBGP session between the peers (MLAG-IPv4-UNDERLAY-PEER) runs over it so a leaf "
                    "keeps reachability if all its uplinks fail.", inputs, D("node-type-l2-mlag-configuration"),
                    related=[r"MLAG-IPv4-UNDERLAY-PEER"], topic="mlag")
    found = n.find_vlan(vid)
    if found and found[4] == "svi":
        key, t, vrf, svi, _k = found
        vmac = n.setting("virtual_router_mac_address")
        why = (f"SVI Vlan{vid} of VRF {vrf['name']} (tenant {t['name']}, svis[id={vid}]). "
               + (f"`ip_address_virtual: {svi['ip_address_virtual']}` renders `ip address virtual` — the same anycast "
                  "gateway IP and MAC on every leaf (EVPN anycast gateway, no per-leaf IPs needed). It requires "
                  f"`virtual_router_mac_address` ({vmac.value})." if svi.get("ip_address_virtual") else
                  "Per-node addressing via `nodes[].ip_address` / `ip_virtual_router_addresses` (VARP)."))
        return Expl(why, [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}].svis[id={vid}]", svi, n.where_of(key),
                                n.lab.knob_if("tenants")), vmac],
                    D("network-services-vrfs-svis-settings"), related=[rf"^vlan {vid}$", r"ip virtual-router mac-address"],
                    topic="services")
    ex = _vlan_expl(n, vid, "SVI Vlan")
    if ex and ex.topic == "mlag":
        _ip, inputs, why = _mlag_ip(n, "mlag_peer_l3_ipv4_pool", "Its address")
        ex.why += " " + why + " Every VRF peering SVI reuses the same /31 as Vlan4093."
    return ex


@rule("svi-child", r"^(ip address virtual \S+|ip address \S+|ip virtual-router address \S+|vrf \S+|description .+|no autostate)$", section=r"^interface Vlan\d+$",
      topic="services")
def _svichild(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    return _svi_expl(n, int(L.header(cl).replace("interface Vlan", "")), m.group(1))


@rule("varp-mac", r"^ip virtual-router mac-address (\S+)$", topic="services")
def _vmac(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Anycast gateway MAC {m.group(1)} = `virtual_router_mac_address` (node setting). Every leaf answers ARP "
                "for the gateway IP with this MAC, so hosts keep the same gateway when they move.",
                [n.setting("virtual_router_mac_address")], D("node-type-l2-mlag-configuration"), related=[r"ip address virtual"])


# ================================================================================================ VXLAN
@rule("vxlan1", r"^interface Vxlan1$", topic="overlay")
def _vxlan1(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Vxlan1 exists because node type `{n.type_key}` is a VTEP (`node_type_keys[].vtep: true`) and the "
                "overlay is EVPN (`overlay_routing_protocol: ebgp`). VLANs map to L2VNIs, VRFs to L3VNIs.",
                [n.fabric("overlay_routing_protocol", "ebgp"), n.setting("vtep")], D("node-type-loopback-vtep-configuration"),
                related=[r"^vxlan "])


@rule("vxlan-desc", r"^description (\S+)$", section=r"^interface Vxlan1$", topic="overlay")
def _vxdesc(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"Description '{m.group(1)}' = `<hostname>_VTEP` (AVD default for the Vxlan1 interface).",
                [Input("inventory hostname", n.host, "inventory.yml")], D("node-type-loopback-vtep-configuration"))


@rule("vxlan-src", r"^vxlan source-interface (\S+)$", section=r"^interface Vxlan1$", topic="overlay")
def _vxsrc(n: NodeCtx, m: M, cl, L) -> Expl:
    ex, _ip = _vtep(n)
    ex.why = f"VXLAN source {m.group(1)} = `vtep_loopback` (default Loopback1). " + ex.why
    return ex


@rule("vxlan-vrmac", r"^vxlan virtual-router encapsulation mac-address mlag-system-id$", section=r"^interface Vxlan1$", topic="overlay")
def _vxmac(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("Added on MLAG VTEPs: routed VXLAN traffic uses the MLAG system-id as inner source MAC so both peers "
                "look like one router behind the shared VTEP IP.", [n.setting("mlag", True)], D("node-type-l2-mlag-configuration"))


@rule("vxlan-udp", r"^vxlan udp-port (\d+)$", section=r"^interface Vxlan1$", topic="overlay")
def _vxudp(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"VXLAN UDP port {m.group(1)} (IANA standard, always rendered).", [], C("vxlan-interface"))


@rule("vxlan-vlan", r"^vxlan vlan (\d+) vni (\d+)$", section=r"^interface Vxlan1$", topic="overlay")
def _vxvlan(n: NodeCtx, m: M, cl, L) -> Expl | None:
    vid, vni = int(m.group(1)), int(m.group(2))
    found = n.find_vlan(vid)
    if not found:
        return None
    key, t, vrf, obj, kind = found
    base = t.get("mac_vrf_vni_base")
    if obj.get("vni_override"):
        why = f"L2VNI {vni} = `vni_override` of VLAN {vid}."
    else:
        why = f"L2VNI {vni} = tenant {t['name']} mac_vrf_vni_base {base} + VLAN id {vid}."
    return Expl(why + " Every VTEP carrying the VLAN maps it to the same VNI; EVPN type-2/3 routes for it use the "
                "VLAN's RD/RT.", [Input(f"{key}[name={t['name']}].mac_vrf_vni_base", base, n.where_of(key), n.lab.knob_if("tenants"))],
                D("network-services"), related=[rf"^vlan {vid}$", rf"^vlan {vid}$"])


@rule("vxlan-vrf", r"^vxlan vrf (\S+) vni (\d+)$", section=r"^interface Vxlan1$", topic="overlay")
def _vxvrf(n: NodeCtx, m: M, cl, L) -> Expl | None:
    f = n.find_vrf(m.group(1))
    if not f:
        return None
    key, t, vrf = f
    src = "vrf_vni" if vrf.get("vrf_vni") is not None else "vrf_id"
    return Expl(f"L3VNI {m.group(2)} of VRF {vrf['name']} = `{src}` ({vrf.get(src)}). Used for EVPN type-5 routes and "
                "symmetric IRB between leafs.", [Input(f"{key}[name={t['name']}].vrfs[name={vrf['name']}].{src}", vrf.get(src),
                                                       n.where_of(key), n.lab.knob_if("tenants"))],
                D("network-services-vrfs-settings"), related=[rf"vrf {re.escape(vrf['name'])}$"])


# ================================================================================================ prefix-lists / route-maps
@rule("pl-loopbacks", r"^ip prefix-list (PL-LOOPBACKS-EVPN-OVERLAY)$", topic="underlay")
def _pllo(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("Prefix-list of the loopback pools (`permit <pool> eq 32`): used by route-map RM-CONN-2-BGP so only "
                "Loopback0/VTEP addresses are redistributed into the eBGP underlay "
                "(`underlay_filter_redistribute_connected: true`, default). If a pool is missing here, the overlay never "
                "comes up.", [n.setting("loopback_ipv4_pool"), n.setting("vtep_loopback_ipv4_pool"),
                              n.fabric("underlay_filter_redistribute_connected", True)], D("fabric-settings"),
                related=[r"PL-LOOPBACKS-EVPN-OVERLAY", r"RM-CONN-2-BGP"])


@rule("pl-seq", r"^seq (\d+) permit (\S+)( eq 32)?$", section=r"^ip prefix-list", topic="underlay")
def _plseq(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    hdr = L.header(cl)
    pfx = m.group(2)
    if "PL-LOOPBACKS-EVPN-OVERLAY" in hdr:
        for key in ("loopback_ipv4_pool", "vtep_loopback_ipv4_pool"):
            s = n.setting(key)
            if s.value and pfx in [p.strip() for p in str(s.value).split(",")]:
                s.knob = n.lab.knob_if("pools")
                return Expl(f"{pfx} eq 32 = `{key}` — every /32 carved from it may be advertised into the underlay.",
                            [s], D("node-type-loopback-vtep-configuration"), related=[r"RM-CONN-2-BGP"])
        # a spine's list contains the pools of every node type sharing it
        return Expl(f"{pfx} eq 32 is one of the loopback pools of the fabric (loopback_ipv4_pool / vtep_loopback_ipv4_pool).",
                    [n.setting("loopback_ipv4_pool")], D("node-type-loopback-vtep-configuration"))
    if "PL-MLAG-PEER-VRFS" in hdr:
        ip, inputs, why = _mlag_ip(n, "mlag_peer_l3_ipv4_pool", "The VRF peering SVI address")
        return Expl(f"{pfx} is the MLAG L3 /31 reused on every VRF peering SVI; RM-CONN-2-BGP-VRFS denies it so it is not "
                    "advertised into EVPN. " + why, inputs, D("node-type-l2-mlag-configuration"))
    return None


@rule("pl-mlag", r"^ip prefix-list (PL-MLAG-PEER-VRFS)$", topic="mlag")
def _plmlag(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("Matches the MLAG L3 /31 that every VRF peering SVI reuses; route-map RM-CONN-2-BGP-VRFS denies it so the "
                "per-VRF `redistribute connected` does not export the peering subnet to EVPN.",
                [n.setting("mlag_peer_l3_ipv4_pool")], D("node-type-l2-mlag-configuration"), related=[r"PL-MLAG-PEER-VRFS"])


@rule("rm-conn", r"^route-map (RM-CONN-2-BGP) permit 10$", topic="underlay")
def _rmconn(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("`redistribute connected route-map RM-CONN-2-BGP` advertises only the loopbacks (match "
                "PL-LOOPBACKS-EVPN-OVERLAY) into the underlay — not the /31 P2P links. Controlled by "
                "`underlay_filter_redistribute_connected` (default true).",
                [n.fabric("underlay_filter_redistribute_connected", True)], D("fabric-settings"),
                related=[r"RM-CONN-2-BGP\b", r"PL-LOOPBACKS"])


@rule("rm-conn-vrfs", r"^route-map (RM-CONN-2-BGP-VRFS) (deny 10|permit 20)$", topic="services")
def _rmconnvrfs(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"RM-CONN-2-BGP-VRFS {m.group(2)}: used by `redistribute connected` in each tenant VRF on MLAG leafs — "
                "deny the MLAG peering /31 (PL-MLAG-PEER-VRFS), permit everything else (the SVI subnets become EVPN "
                "type-5 routes).", [n.setting("mlag_peer_l3_ipv4_pool")], D("network-services-vrfs-settings"),
                related=[r"RM-CONN-2-BGP-VRFS", r"PL-MLAG-PEER-VRFS"])


@rule("rm-mlag-in", r"^route-map (RM-MLAG-PEER-IN) permit 10$", topic="mlag")
def _rmmlag(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("Inbound on MLAG-IPv4-UNDERLAY-PEER: routes learned from the MLAG peer get `origin incomplete` so paths "
                "via the spines are preferred and the peer-link is a backup path (`mlag_ibgp_origin_incomplete: true`, "
                "default).", [n.setting("mlag_ibgp_origin_incomplete", True)], D("node-type-l2-mlag-configuration"),
                related=[r"RM-MLAG-PEER-IN"])


@rule("rm-child", r"^(match ip address prefix-list \S+|set origin incomplete|description .*)$", section=r"^route-map ", topic="routing")
def _rmchild(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    hdr = L.header(cl)
    for rid, fn in (("RM-CONN-2-BGP-VRFS", _rmconnvrfs), ("RM-CONN-2-BGP ", _rmconn), ("RM-MLAG-PEER-IN", _rmmlag)):
        if rid in hdr + " ":
            mm = re.search(r"^route-map (\S+) (deny 10|permit 20|permit 10)", hdr)
            return fn(n, mm, cl, L) if mm else None
    return None


# ================================================================================================ MLAG
@rule("mlag-config", r"^mlag configuration$", topic="mlag")
def _mlagcfg(n: NodeCtx, m: M, cl, L) -> Expl:
    ng = n.node_group or {}
    return Expl(f"MLAG is automatic: `{n.type_key}` has `mlag_support: true`, and node_group {ng.get('group')} contains "
                f"exactly two nodes ({[x.get('name') for x in ng.get('nodes', [])]}). The first node listed is the MLAG "
                "primary. A group with 1 or 3+ nodes gets no MLAG.",
                [Input(f"{n.type_key}.node_groups[group={ng.get('group')}].nodes", [x.get("name") for x in ng.get("nodes", [])],
                       n.setting("id").where)], D("node-type-l2-mlag-configuration"),
                related=[r"^interface Vlan4094$", r"^interface Port-Channel", r"MLAG"])


@rule("mlag-domain", r"^domain-id (\S+)$", section=r"^mlag configuration$", topic="mlag")
def _mlagdom(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"domain-id {m.group(1)} = the node_group name (`mlag_domain_id` overrides it).",
                [Input(f"{n.type_key}.node_groups[group={n.group_name}].group", n.group_name, n.setting("id").where),
                 n.setting("mlag_domain_id")], D("node-type-l2-mlag-configuration"))


@rule("mlag-local", r"^local-interface (\S+)$", section=r"^mlag configuration$", topic="mlag")
def _mlaglocal(n: NodeCtx, m: M, cl, L) -> Expl:
    ip, inputs, why = _mlag_ip(n, "mlag_peer_ipv4_pool", f"{m.group(1)}")
    return Expl(f"MLAG control traffic is sourced from {m.group(1)} (`mlag_peer_vlan`, default 4094). " + why, inputs,
                D("node-type-l2-mlag-configuration"), related=[rf"^interface {m.group(1)}$"])


@rule("mlag-peer-address", r"^peer-address (\S+)$", section=r"^mlag configuration$", topic="mlag")
def _mlagpeer(n: NodeCtx, m: M, cl, L) -> Expl:
    _ip, inputs, why = _mlag_ip(n, "mlag_peer_ipv4_pool", "This node's Vlan4094")
    return Expl(f"peer-address {m.group(1)} is {n.mlag_peer()}'s Vlan4094 address. " + why, inputs,
                D("node-type-l2-mlag-configuration"))


@rule("mlag-peer-link", r"^peer-link (\S+)$", section=r"^mlag configuration$", topic="mlag")
def _mlagpl(n: NodeCtx, m: M, cl, L) -> Expl:
    pid, inp = _mlag_po_id(n)
    return Expl(f"peer-link {m.group(1)}: the Port-Channel built from `mlag_interfaces` (id = number of the first member "
                "unless `mlag_port_channel_id` is set).", [n.setting("mlag_interfaces"), n.setting("mlag_port_channel_id")],
                D("node-type-l2-mlag-configuration"), related=[rf"^interface {m.group(1)}$"])


@rule("mlag-reload", r"^reload-delay (mlag|non-mlag) (\d+)$", section=r"^mlag configuration$", topic="mlag")
def _mlagrd(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"reload-delay {m.group(1)} {m.group(2)} s comes from `platform_settings[].reload_delay` for platform "
                f"`{n.setting('platform').value}` (300/330 for cEOSLab/vEOS and most fixed platforms, 900/1020 on 7280R/7500R).",
                [n.setting("platform")], D("platform-settings"))


# ================================================================================================ BGP
@rule("router-bfd", r"^router bfd$", topic="routing")
def _bfd(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("`router bfd` multihop timers from `bfd_multihop` (default interval 300 / min-rx 300 / multiplier 3). "
                "Used by the EVPN overlay sessions (`bgp_peer_groups.evpn_overlay_peers.bfd: true`). On slow virtual "
                "labs raise them (e.g. 1200).", [n.fabric("bfd_multihop", {"interval": 300, "min_rx": 300, "multiplier": 3})],
                D("bfd-settings"), related=[r"^multihop", r"EVPN-OVERLAY-PEERS bfd"])


@rule("router-bfd-mh", r"^multihop interval (\d+) min-rx (\d+) multiplier (\d+)$", section=r"^router bfd$", topic="routing")
def _bfdmh(n: NodeCtx, m: M, cl, L) -> Expl:
    return _bfd(n, m, cl, L)


@rule("router-bgp", r"^router bgp (\S+)$", topic="routing")
def _rbgp(n: NodeCtx, m: M, cl, L) -> Expl:
    asn = n.setting("bgp_as")
    asn.knob = n.lab.knob_if("spine_asn" if n.type_key == "spine" else "leaf_asn_base")
    extra = " MLAG peers must share one AS, so it is set on the node_group." if n.mlag_peer() else ""
    return Expl(f"BGP AS {m.group(1)} = `bgp_as` of this node ({asn.key}).{extra} eBGP underlay+overlay: spines share one "
                "AS, every leaf pair gets its own.", [asn], D("node-type-bgp-configuration"), related=[r"remote-as"])


@rule("bgp-router-id", r"^router-id (\S+)$", section=r"^router bgp ", topic="routing")
def _bgprid(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    ex, ip = _lo0(n)
    vrf = L.parent(cl)
    pre = (f"VRF {vrf.split()[1]} uses the main router-id (`vrfs[].bgp.router_id: main_router_id`, default). "
           if vrf.startswith("vrf ") else "")
    ex.why = pre + f"BGP router-id {m.group(1)} = Loopback0. " + ex.why
    return ex


@rule("bgp-no-default-ipv4", r"^no bgp default ipv4-unicast$", section=r"^router bgp ", topic="routing")
def _bgpnodef(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("`bgp_default_ipv4_unicast: false` (default): neighbors are not activated for IPv4 automatically — each "
                "peer group is activated explicitly under `address-family ipv4` / `address-family evpn`.",
                [n.fabric("bgp_default_ipv4_unicast", False)], D("bgp-settings"), related=[r"activate$"])


@rule("bgp-maxpaths", r"^maximum-paths (\d+)( ecmp (\d+))?$", section=r"^router bgp ", topic="routing")
def _bgpmp(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"ECMP over {m.group(1)} paths = `bgp_maximum_paths` (default 4){'; ecmp ' + m.group(3) + ' = bgp_ecmp' if m.group(3) else ''}. "
                "With 2 spines a leaf load-balances over both uplinks.",
                [n.fabric("bgp_maximum_paths", 4), n.fabric("bgp_ecmp")], D("bgp-settings"))


PG_KEYS = {"IPv4-UNDERLAY-PEERS": "ipv4_underlay_peers", "MLAG-IPv4-UNDERLAY-PEER": "mlag_ipv4_underlay_peer",
           "EVPN-OVERLAY-PEERS": "evpn_overlay_peers", "EVPN-OVERLAY-CORE": "evpn_overlay_core",
           "MPLS-OVERLAY-PEERS": "mpls_overlay_peers", "RR-OVERLAY-PEERS": "rr_overlay_peers",
           "IPVPN-GATEWAY-PEERS": "ipvpn_gateway_peers", "WAN-OVERLAY-PEERS": "wan_overlay_peers"}
PG_ROLE = {"ipv4_underlay_peers": "the eBGP underlay sessions on the P2P links",
           "mlag_ipv4_underlay_peer": "the iBGP session to the MLAG peer over Vlan4093",
           "evpn_overlay_peers": "the EVPN overlay sessions between loopbacks (leafs ↔ route servers)",
           "evpn_overlay_core": "EVPN gateway sessions to remote domains", "mpls_overlay_peers": "MPLS overlay (iBGP)",
           "rr_overlay_peers": "sessions between route reflectors", "ipvpn_gateway_peers": "EVPN↔IP-VPN gateway sessions",
           "wan_overlay_peers": "WAN overlay sessions"}
PG_ATTR = {
    "peer group": "declares the peer group",
    "update-source Loopback0": "overlay sessions run between Loopback0 addresses (multi-hop)",
    "bfd": "BFD on the sessions (`bgp_peer_groups.<pg>.bfd`, default true for overlay groups)",
    "ebgp-multihop": "loopback-to-loopback eBGP needs multihop (`evpn_ebgp_multihop`, default 3)",
    "password 7": "type-7 password (`bgp_peer_groups.<pg>.password`, encrypted for this peer-group name; "
                  "the device_password knob sets it in the lab)",
    "send-community": "standard + extended communities (EVPN RTs travel as extended communities)",
    "maximum-routes": "`maximum_routes` (underlay default 256000 since 6.0; overlay 0 = unlimited)",
    "next-hop-unchanged": "route servers must not rewrite the next hop, so EVPN routes keep the originating VTEP "
                          "(`evpn_role: server` on spines)",
    "next-hop-self": "the MLAG peer advertises its own address for routes it relays",
    "remote-as": "iBGP to the MLAG peer: same AS as this node",
    "description": "the MLAG peer's hostname",
    "route-map RM-MLAG-PEER-IN in": "prefer spine paths over the peer-link (origin incomplete)",
}


@rule("bgp-pg", r"^neighbor ([A-Za-z][\w-]+) (.+)$", section=r"^router bgp ", topic="routing")
def _bgppg(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    pg, rest = m.group(1), m.group(2)
    key = PG_KEYS.get(pg)
    if not key:
        return None
    parent = L.parent(cl)
    if parent.startswith(("address-family", "vrf ")):
        return None
    attr = next((v for k, v in PG_ATTR.items() if rest.startswith(k)), None)
    inp = n.fabric(f"bgp_peer_groups.{key}")
    if "password" in rest:
        inp.knob = n.lab.knob_if("device_password")
    txt = f"Peer group {pg} (`bgp_peer_groups.{key}`) carries {PG_ROLE[key]}."
    if attr:
        txt += f" `{rest.split(' ')[0] if rest != 'peer group' else rest}`: {attr}."
    if rest.startswith("ebgp-multihop"):
        inp = n.fabric("evpn_ebgp_multihop", 3)
    return Expl(txt, [inp], D("bgp-settings"), related=[rf"peer group {re.escape(pg)}$"])


def _neighbor(n: NodeCtx, ip: str, vrf: str | None) -> dict[str, Any] | None:
    rb = n.sc.get("router_bgp") or {}
    items = rb.get("neighbors") or []
    if vrf:
        v = next((x for x in rb.get("vrfs") or [] if x.get("name") == vrf), {})
        items = v.get("neighbors") or []
    return next((x for x in items if x.get("ip_address") == ip), None)


@rule("bgp-neighbor", r"^neighbor (\d+\.\d+\.\d+\.\d+) (peer group \S+|remote-as \S+|description \S+)$", section=r"^router bgp ",
      topic="routing")
def _bgpnb(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    ip = m.group(1)
    parent = L.parent(cl)
    vrf = parent.split()[1] if parent.startswith("vrf ") else None
    nb = _neighbor(n, ip, vrf) or {}
    pg = nb.get("peer_group") or ""
    peer = nb.get("peer") or str(nb.get("description") or "").split("_")[0]
    if pg == "EVPN-OVERLAY-PEERS":
        role = n.setting("evpn_role").value
        if n.type_key == "spine" or role == "server":
            why = (f"EVPN overlay session to {peer} Loopback0 {ip} (remote-as {nb.get('remote_as')}). This node is an "
                   "EVPN route server (`evpn_role: server`, default for spines); it peers with every client that lists it "
                   "in `evpn_route_servers` and relays routes with next-hop unchanged.")
            inputs = [n.setting("evpn_role", "server")]
        else:
            rs = n.setting("evpn_route_servers")
            why = (f"EVPN overlay session to route server {peer} (its Loopback0 {ip}, remote-as {nb.get('remote_as')} = the "
                   f"spine bgp_as). `evpn_route_servers` defaults to `uplink_switches` "
                   f"{n.setting('uplink_switches').value} for `evpn_role: client` nodes.")
            inputs = [rs if rs.value else n.setting("uplink_switches"), n.setting("evpn_role", "client")]
        return Expl(why, inputs, D("node-type-bgp-configuration"), related=[rf"\b{re.escape(ip)}\b"])
    if pg == "IPv4-UNDERLAY-PEERS":
        eth = next((e for e in n.sc.get("ethernet_interfaces") or [] if (e.get("metadata") or {}).get("peer") == peer
                    and e.get("ip_address")), None)
        ex = _p2p(n, eth["name"]) if eth else None
        why = (f"eBGP underlay neighbor {ip} = {peer}'s end of the P2P /31 (remote-as {nb.get('remote_as')} = {peer}'s "
               "bgp_as). One session per fabric link, BFD-free, only loopbacks are exchanged.")
        if ex:
            ex.why = why + " " + ex.why
            return ex
        return Expl(why, [n.setting("uplink_ipv4_pool")], D("node-type-uplink-configuration"))
    if pg == "MLAG-IPv4-UNDERLAY-PEER":
        pool = "mlag_peer_l3_ipv4_pool"
        _ip, inputs, why = _mlag_ip(n, pool, "This node's peering SVI")
        where = f" inside VRF {vrf} (per-VRF peering SVI)" if vrf else " over Vlan4093"
        return Expl(f"iBGP to the MLAG peer {n.mlag_peer()} at {ip}{where}. " + why, inputs, D("node-type-l2-mlag-configuration"),
                    related=[rf"\b{re.escape(ip)}\b"])
    return None


@rule("bgp-redistribute", r"^redistribute connected route-map (\S+)$", section=r"^router bgp ", topic="routing")
def _bgpredist(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    if m.group(1) == "RM-CONN-2-BGP-VRFS":
        return _rmconnvrfs(n, re.match(r"route-map (RM-CONN-2-BGP-VRFS) (permit 20)", "route-map RM-CONN-2-BGP-VRFS permit 20"), cl, L)
    return _rmconn(n, m, cl, L)


def _vlan_rd_rt(n: NodeCtx, vid: int) -> Expl | None:
    found = n.find_vlan(vid)
    if not found:
        return None
    key, t, vrf, obj, kind = found
    rid = n.router_id()
    base = t.get("mac_vrf_id_base", t.get("mac_vrf_vni_base"))
    num = _int(base) + vid
    return Expl(f"MAC-VRF of VLAN {vid}: RD {rid}:{num} = Loopback0 {rid} : (mac_vrf_id_base|mac_vrf_vni_base {base} + "
                f"{vid}); route-target both {num}:{num}. RD makes the routes unique per VTEP, the RT is shared fabric-wide "
                "so all VTEPs import each other's MAC/IP routes. `redistribute learned` advertises locally learned MACs "
                "as EVPN type-2 routes.", [Input(f"{key}[name={t['name']}].mac_vrf_vni_base", t.get("mac_vrf_vni_base"),
                                                  n.where_of(key), n.lab.knob_if("tenants")),
                                            n.fabric("overlay_rd_type"), n.fabric("overlay_rt_type")],
                D("overlay-settings"), related=[rf"vxlan vlan {vid} "])


@rule("bgp-vlan", r"^vlan (\d+)$", section=r"^router bgp ", topic="overlay")
def _bgpvlan(n: NodeCtx, m: M, cl, L) -> Expl | None:
    return _vlan_rd_rt(n, int(m.group(1)))


@rule("bgp-vlan-child", r"^(rd \S+|route-target both \S+|redistribute learned)$", section=r"^router bgp ", parent=r"^vlan \d+$",
      topic="overlay")
def _bgpvlanchild(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    return _vlan_rd_rt(n, int(L.parent(cl).split()[1]))


def _vrf_rd_rt(n: NodeCtx, name: str) -> Expl | None:
    f = n.find_vrf(name)
    if not f:
        return None
    key, t, vrf = f
    vid = _int(vrf.get("vrf_id", vrf.get("vrf_vni")))
    rid = n.router_id()
    return Expl(f"BGP VRF {name}: RD {rid}:{vid} = Loopback0 {rid} : vrf_id {vid} (vrf_id defaults to vrf_vni); "
                f"route-target import/export evpn {vid}:{vid}. All leafs carrying {name} share the RT, so EVPN type-5 "
                "(IP prefix) routes are imported into the right VRF.",
                [Input(f"{key}[name={t['name']}].vrfs[name={name}].vrf_vni|vrf_id", vid, n.where_of(key), n.lab.knob_if("tenants")),
                 n.fabric("overlay_rd_type")], D("overlay-settings"), related=[rf"vxlan vrf {re.escape(name)} "])


@rule("bgp-vrf", r"^vrf (\S+)$", section=r"^router bgp ", topic="overlay")
def _bgpvrf(n: NodeCtx, m: M, cl, L) -> Expl | None:
    return _vrf_rd_rt(n, m.group(1))


@rule("bgp-vrf-child", r"^(rd \S+|route-target (import|export) evpn \S+)$", section=r"^router bgp ", parent=r"^vrf \S+$", topic="overlay")
def _bgpvrfchild(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    return _vrf_rd_rt(n, L.parent(cl).split()[1])


@rule("bgp-af", r"^address-family (evpn|ipv4)$", section=r"^router bgp ", topic="routing")
def _bgpaf(n: NodeCtx, m: M, cl, L) -> Expl:
    if m.group(1) == "evpn":
        return Expl("The EVPN address family is activated only on EVPN-OVERLAY-PEERS: MAC/IP (type-2), IMET (type-3) and "
                    "IP-prefix (type-5) routes travel on the loopback-to-loopback overlay sessions.",
                    [n.fabric("overlay_routing_protocol", "ebgp")], D("overlay-settings"))
    return Expl("IPv4 unicast is activated on the underlay peer groups only (and explicitly de-activated on the overlay "
                "group) — the underlay carries loopback reachability, the overlay carries EVPN.",
                [n.fabric("underlay_routing_protocol", "ebgp")], D("fabric-settings"))


@rule("bgp-af-activate", r"^(no )?neighbor (\S+) activate$", section=r"^router bgp ", parent=r"^address-family", topic="routing")
def _bgpact(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    af = L.parent(cl).split()[1]
    return Expl(f"{'De-activates' if m.group(1) else 'Activates'} {m.group(2)} for address-family {af} "
                "(needed because `no bgp default ipv4-unicast`).",
                [n.fabric("bgp_default_ipv4_unicast", False)], D("bgp-settings"))


# ================================================================================================ CloudVision / MSS
@rule("terminattr", r"^daemon TerminAttr$", topic="cloudvision")
def _ta(n: NodeCtx, m: M, cl, L) -> Expl:
    cvs = n.fabric("cv_settings")
    cvs.knob = n.lab.knob_if("cloudvision")
    return Expl("TerminAttr streams telemetry to CloudVision. Rendered from `cv_settings` (CVaaS clusters or on-prem "
                "`onprem_clusters`), which replaced the 5.x `cvp_instance_ips`/`terminattr_*` keys. CloudVision needs NTP "
                "(and DNS for CVaaS) — AVD refuses cv_settings without ntp_settings.servers.", [cvs], D("cloudvision-settings"),
                related=[r"TerminAttr", r"^ntp server"])


@rule("terminattr-exec", r"^exec /usr/bin/TerminAttr (.*)$", section=r"^daemon TerminAttr$", topic="cloudvision")
def _taexec(n: NodeCtx, m: M, cl, L) -> Expl:
    args = m.group(1)
    parts = []
    mm = re.search(r"-cvaddr=(\S+)", args)
    if mm:
        parts.append(f"-cvaddr={mm.group(1)} (the cluster address; apiserver.arista.io:443 for CVaaS region auto, "
                     "<server>:9910 on-prem)")
    mm = re.search(r"-cvauth=(\S+)", args)
    if mm:
        parts.append(f"-cvauth={mm.group(1)} (onboarding token file, `token_file`: /tmp/cv-onboarding-token for CVaaS, "
                     "/tmp/token on-prem)")
    if "-cvvrf=" in args:
        parts.append("-cvvrf = the management VRF (`vrf: use_mgmt_interface_vrf`)")
    if "-disableaaa" in args:
        parts.append("-disableaaa (`cv_settings.terminattr.disable_aaa`)")
    if "-smashexcludes" in args:
        parts.append("-smashexcludes / -ingestexclude = AVD defaults to limit streamed state")
    return Expl("TerminAttr arguments generated from `cv_settings`: " + "; ".join(parts) + ".",
                [n.fabric("cv_settings")], D("cloudvision-settings"))


@rule("terminattr-noshut", r"^no shutdown$", section=r"^daemon TerminAttr$", topic="cloudvision")
def _tanoshut(n: NodeCtx, m: M, cl, L) -> Expl:
    return _ta(n, m, cl, L)


@rule("segment-security", r"^router segment-security$", topic="segmentation")
def _mss(n: NodeCtx, m: M, cl, L) -> Expl:
    seg = Input("segmentation", n.lab.knobs.get("segmentation"), "lab knob (Settings → Segmentation)", n.lab.knob_if("segmentation"))
    return Expl("Multi-domain segmentation (MSS foundation): the lab's `segmentation` knob is rendered into the node-level "
                "`structured_config.router_segment_security` of every L3 leaf that carries one of the segmented VRFs "
                "(spines and L2 leafs stay untouched). Segments = prefixes (match-lists) per VRF; policies decide which "
                "applications may flow from one segment to another; `fallback` covers everything else.",
                [seg, n.setting("structured_config")], C("router-segment-security"),
                related=[r"segment ", r"policy ", r"match-list input prefix-ipv4 MSS-"])


@rule("segment-child", r"^(policy \S+|vrf \S+|segment \S+|\d+ application \S+ action \S+.*|from \S+ policy \S+|fallback policy \S+|match prefix-ipv4 \S+|definition|policies|no shutdown)$",
      section=r"^router segment-security$", topic="segmentation")
def _msschild(n: NodeCtx, m: M, cl, L) -> Expl:
    ex = _mss(n, m, cl, L)
    line = m.group(1)
    if line.startswith("fallback"):
        ex.why = f"`{line}`: segment `fallback: forward|drop` → policy-forward-all / policy-drop-all. " + ex.why
    elif line.startswith("from "):
        ex.why = f"`{line}`: traffic arriving from that segment is checked against the named policy. " + ex.why
    return ex


@rule("mss-matchlist", r"^match-list input prefix-ipv4 (MSS-\S+)$", topic="segmentation")
def _mssml(n: NodeCtx, m: M, cl, L) -> Expl:
    ex = _mss(n, m, cl, L)
    ex.why = f"{m.group(1)} = the prefixes of one segment (MSS-<VRF>-<SEGMENT>). " + ex.why
    return ex


@rule("mss-matchlist-child", r"^match prefix-ipv4 (\S+)$", section=r"^match-list input prefix-ipv4 MSS-", topic="segmentation")
def _mssmlchild(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    ex = _mss(n, m, cl, L)
    ex.why = f"{m.group(1)} is one of the `prefixes` of segment {L.header(cl).split('-')[-1]}. " + ex.why
    return ex


@rule("mss-atr-child", r"^(application l4 \S+|protocol .*|field-set l4-port \S+|[\d, -]+)$", section=r"^application traffic recognition$",
      topic="segmentation")
def _mssatrchild(n: NodeCtx, m: M, cl, L) -> Expl:
    return _mssatr(n, m, cl, L)


@rule("mss-atr", r"^application traffic recognition$", topic="segmentation")
def _mssatr(n: NodeCtx, m: M, cl, L) -> Expl:
    ex = _mss(n, m, cl, L)
    ex.why = "Applications (L4 protocol + port field-sets MSS-<APP>) referenced by segment-security policies. " + ex.why
    return ex



# ================================================================================================ IGPs / MPLS (other designs)
def _igp(n: NodeCtx) -> Input:
    return n.fabric("underlay_routing_protocol", "(node type default)")


@rule("router-ospf", r"^router ospf (\d+)$", topic="underlay")
def _rospf(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"OSPF process {m.group(1)} is the underlay (`underlay_routing_protocol: {_igp(n).value}`; process id = "
                "`underlay_ospf_process_id`, default 100, area `underlay_ospf_area` 0.0.0.0). AVD makes every interface "
                "passive except the fabric links and redistributes connected (loopbacks).",
                [_igp(n), n.fabric("underlay_ospf_process_id", 100), n.fabric("underlay_ospf_area", "0.0.0.0")],
                D("ospf-settings"), related=[r"ip ospf"])


@rule("router-ospf-child", r"^(router-id \S+|passive-interface default|no passive-interface \S+|redistribute connected.*|max-lsa \d+|bfd default|timers .*|graceful-restart.*|maximum-paths \d+)$",
      section=r"^router ospf ", topic="underlay")
def _rospfchild(n: NodeCtx, m: M, cl, L) -> Expl:
    line = m.group(1)
    extra = {"max-lsa": " (`underlay_ospf_max_lsa`, default 12000)", "router-id": " (= Loopback0)",
             "no passive-interface": " — an underlay link (uplink / MLAG L3 / core link with include_in_underlay_protocol)"}
    add = next((v for k, v in extra.items() if line.startswith(k)), "")
    ex = _rospf(n, re.match(r"router ospf (\d+)", L.header(cl)), cl, L)
    ex.why = f"`{line}`{add}. " + ex.why
    return ex


@rule("intf-ospf", r"^ip ospf (network point-to-point|area \S+|cost \d+|authentication.*|message-digest-key.*)$",
      section=r"^interface ", topic="underlay")
def _intfospf(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"`ip ospf {m.group(1)}`: this interface takes part in the OSPF underlay (area `underlay_ospf_area`, "
                "point-to-point on fabric links).", [_igp(n), n.fabric("underlay_ospf_area", "0.0.0.0")], D("ospf-settings"))


@rule("router-isis", r"^router isis (\S+)$", topic="underlay")
def _risis(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"IS-IS instance {m.group(1)} (`underlay_isis_instance_name`) is the underlay "
                f"(`underlay_routing_protocol: {_igp(n).value}`); NET from `isis_area_id` (default 49.0001) + a system id "
                "derived from the loopback (`isis_system_id_format`); SR/LDP variants add MPLS.",
                [_igp(n), n.fabric("isis_area_id", "49.0001")], D("isis-settings"), related=[r"^isis ", r"isis enable"])


@rule("router-isis-child", r".+", section=r"^router isis ", topic="underlay")
def _risischild(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    ex = _risis(n, re.match(r"router isis (\S+)", L.header(cl)), cl, L)
    ex.why = f"`{cl.stripped}` is part of the IS-IS underlay block. " + ex.why
    return ex


@rule("intf-isis", r"^(isis .+|mpls ip|mpls ldp .+|no mpls ip)$", section=r"^interface ", topic="underlay")
def _intfisis(n: NodeCtx, m: M, cl, L) -> Expl:
    what = "MPLS forwarding (`mpls_lsr: true` node types p/pe/rr, LDP/SR per underlay_routing_protocol)" if "mpls" in m.group(1) \
        else "IS-IS on this interface (metric `isis_default_metric`, circuit type `isis_default_circuit_type`)"
    return Expl(f"`{m.group(1)}`: {what}. The underlay is `{_igp(n).value}`.", [_igp(n)], D("isis-settings"))


@rule("mpls-ldp-child", r".+", section=r"^(mpls ldp|router traffic-engineering)$", topic="underlay")
def _mplschild(n: NodeCtx, m: M, cl, L: Lines) -> Expl:
    ex = _mplsg(n, re.match(r"(.+)", L.header(cl)), cl, L)
    ex.why = f"`{cl.stripped}` (LDP/TE transport, sourced from Loopback0). " + ex.why
    return ex


@rule("mpls-global", r"^(mpls ip|mpls ldp|router traffic-engineering)$", topic="underlay")
def _mplsg(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"`{m.group(1)}`: node type `{n.type_key}` is an MPLS LSR (`node_type_keys[].mpls_lsr`); the label "
                f"distribution follows `underlay_routing_protocol: {_igp(n).value}` (isis-sr / isis-ldp / ospf-ldp …).",
                [_igp(n)], D("node-type-mpls-configuration"))


# ================================================================================================ AAA / dot1x
@rule("aaa", r"^(aaa .+|radius-server .+|tacacs-server .+|ip radius .+|ip tacacs .+)$", topic="system")
def _aaa(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"`{m.group(1)}` comes from `aaa_settings` (authentication / authorization / accounting methods, radius "
                "and tacacs servers). Since AVD 6.0 dot1x AAA moved to `dot1x_settings`.",
                [n.fabric("aaa_settings"), n.fabric("dot1x_settings")], D("aaa-settings"))


@rule("dot1x", r"^dot1x.*$", topic="endpoints")
def _dot1x(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("802.1X from `dot1x_settings` (global) and the port's adapter / network_ports `dot1x` settings.",
                [n.fabric("dot1x_settings")], D("dot1x-settings"))


# ================================================================================================ EVPN gateway (multi-domain)
@rule("evpn-gw-neighbor", r"^neighbor (\d+\.\d+\.\d+\.\d+) (peer group EVPN-OVERLAY-CORE|remote-as \S+|description \S+)$",
      section=r"^router bgp ", topic="overlay")
def _evpngw(n: NodeCtx, m: M, cl, L) -> Expl | None:
    nb = _neighbor(n, m.group(1), None) or {}
    if nb.get("peer_group") != "EVPN-OVERLAY-CORE":
        return None
    gw = n.setting("evpn_gateway")
    return Expl(f"EVPN gateway session to {nb.get('description', m.group(1))} in the remote domain (remote-as "
                f"{nb.get('remote_as')}): `evpn_gateway.remote_peers` on this node. Peer group EVPN-OVERLAY-CORE "
                "(`evpn_ebgp_gateway_multihop`, default 15) interconnects EVPN domains (DCI); routes are re-originated "
                "with `domain remote` RD/RTs.", [gw], D("node-type-evpn-multi-domain-gateway-configuration"),
                related=[r"domain remote", r"EVPN-OVERLAY-CORE"])


@rule("evpn-gw-domain", r"^(rd evpn domain remote \S+|route-target import export evpn domain remote \S+|neighbor EVPN-OVERLAY-CORE .+|neighbor default next-hop-self .+)$",
      section=r"^router bgp ", topic="overlay")
def _evpngwdom(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl(f"`{m.group(1)}`: EVPN multi-domain gateway — `evpn_gateway.evpn_l2.enabled` re-advertises MAC-VRFs to "
                "the remote domain with their own `domain remote` RD/RT, `evpn_gateway.evpn_l3` does the same for "
                "IP prefixes (`inter-domain`, next-hop-self).", [n.setting("evpn_gateway")],
                D("node-type-evpn-multi-domain-gateway-configuration"))


# ================================================================================================ generic interface children
@rule("intf-generic", r".+", section=r"^interface (Ethernet|Port-Channel)", topic="interfaces")
def _intfgeneric(n: NodeCtx, m: M, cl, L: Lines) -> Expl | None:
    ex = _iface_expl(n, L.header(cl).split()[1])
    if ex:
        ex.why = f"`{cl.stripped}` belongs to {L.header(cl).split()[1]}: " + ex.why
    return ex

# ================================================================================================ fallbacks
@rule("end", r"^end$")
def _end(n: NodeCtx, m: M, cl, L) -> Expl:
    return Expl("End of the configuration rendered by eos_cli_config_gen (`config_end: true`, set by eos_designs). "
                "The whole file replaces the running config on deploy (eos_config `replace: config`).", [], C("config-end"))
