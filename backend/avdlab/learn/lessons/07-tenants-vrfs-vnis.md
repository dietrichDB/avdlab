---
id: tenants-vrfs-vnis
order: 7
title: Tenants, VRFs, SVIs, VNIs, RD/RT
level: core
minutes: 20
requires: [built]
tags: [network-services, evpn, vxlan]
summary: From TENANT1 in NETWORK_SERVICES.yml to vlan 11, Vlan11, VNI 10011, RD 10.255.0.3:10011 and VRF10's L3VNI.
---
Network services are defined once, as **tenants** (`group_vars/NETWORK_SERVICES.yml`, knob **Tenants**):

```yaml
tenants:
  - name: TENANT1
    mac_vrf_vni_base: 10000
    vrfs:
      - name: VRF10
        vrf_vni: 10
        svis:
          - id: 11
            name: VRF10_VLAN11
            enabled: true
            ip_address_virtual: 10.10.11.1/24
    l2vlans:
      - id: 3401
        name: L2_VLAN3401
```

From that, every leaf whose filter matches (by default: all tenants, all tags) renders:

| Input | dc1-leaf1a CLI | Rule |
|---|---|---|
| svi id 11, name | `vlan 11 / name VRF10_VLAN11` | the VLAN |
| `ip_address_virtual` | `interface Vlan11 / vrf VRF10 / ip address virtual 10.10.11.1/24` | anycast gateway: same IP **and** MAC on every leaf |
| `virtual_router_mac_address` | `ip virtual-router mac-address 00:1c:73:00:00:99` | the shared gateway MAC |
| `mac_vrf_vni_base` + id | `vxlan vlan 11 vni 10011` | **L2VNI** = 10000 + 11 |
| same number | `router bgp … vlan 11 / rd 10.255.0.3:10011 / route-target both 10011:10011` | MAC-VRF: RD = Loopback0:number, RT = number:number |
| `vrf_vni: 10` | `vxlan vrf VRF10 vni 10` | **L3VNI** for routed (type-5 / symmetric IRB) traffic |
| VRF | `vrf VRF10 / rd 10.255.0.3:10 / route-target import evpn 10:10` | IP-VRF: RD = Loopback0:vrf_id (vrf_id defaults to vrf_vni) |
| l2vlan 3401 | `vlan 3401`, `vxlan vlan 3401 vni 13401`, RD …:13401 | L2 only, still stretched |

**RD vs RT:** the *route distinguisher* makes each VTEP's routes unique (it contains dc1-leaf1a's own Loopback0
10.255.0.3), the *route target* is identical everywhere (10011:10011) — it decides who **imports** the route. Every leaf
with VLAN 11 imports every other leaf's VLAN 11 MACs.

**Classic pitfall:** an SVI without `enabled: true` is rendered **shut down** — AVD's default for `enabled` is false.
And the MLAG pair also gets a per-VRF iBGP peering VLAN (3009 for VRF10) so each peer can route to hosts behind the other.

:::try
id: edit-tenants
label: Open the tenants knob in Settings
action: open_tab
args: {tab: settings}
:::

:::try
id: explain-vni
label: Explain "vxlan vlan 11 vni 10011" on dc1-leaf1a
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: "   vxlan vlan 11 vni 10011"}
:::

:::try
id: open-services
label: Open group_vars/NETWORK_SERVICES.yml
action: open_file
args: {path: group_vars/NETWORK_SERVICES.yml}
:::

:::check
id: l2vni
label: VLAN 11 maps to VNI mac_vrf_vni_base + 11 on dc1-leaf1a
type: config_contains
args: {node: dc1-leaf1a, pattern: "^   vxlan vlan 11 vni 10011$"}
:::

:::check
id: anycast
label: Vlan11 uses the anycast gateway on dc1-leaf2a too
type: config_contains
args: {node: dc1-leaf2a, pattern: "^interface Vlan11\\n(?:   .*\\n)*?   ip address virtual 10\\.10\\.11\\.1/24$"}
:::

:::check
id: vrf-rd
label: VRF10's RD on dc1-leaf2a starts with its own Loopback0 (10.255.0.5)
type: config_contains
args: {node: dc1-leaf2a, pattern: "^   vrf VRF10\\n      rd 10\\.255\\.0\\.5:10$"}
:::

:::quiz
id: q-rt
question: dc1-leaf1a and dc1-leaf2a both carry VLAN 11. Which values are identical on both?
options:
  - RD and RT
  - The RT (10011:10011) and the VNI (10011) — the RD differs because it embeds each leaf's Loopback0
  - Only the VLAN id
answer: 1
explanation: RD = Loopback0:10011 (10.255.0.3:10011 vs 10.255.0.5:10011). RT and VNI must match fabric-wide or the leafs would not import each other's routes.
:::

:::quiz
id: q-enabled
question: You add an SVI without `enabled:`. What does the config show?
options:
  - The SVI with `no shutdown`
  - The SVI with `shutdown`
  - No SVI at all
answer: 1
explanation: eos_designs renders shutdown = not enabled, and enabled defaults to false for SVIs. Always set enabled true.
:::

**Takeaway:** one tenant definition fans out into VLANs, anycast SVIs, L2VNIs (*base + VLAN*), L3VNIs (*vrf_vni*),
and EVPN RD (*Loopback0:number*, unique per leaf) / RT (*number:number*, shared). Change the tenant, rebuild — every
leaf follows.
