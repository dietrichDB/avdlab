---
id: mlag
order: 5
title: MLAG in AVD
level: core
minutes: 15
requires: [built]
tags: [mlag, l2]
summary: What AVD generates for the dc1-leaf1a/b pair — peer-link, peer VLANs, iBGP peering, and dual-homed port-channels.
---
MLAG lets two switches act as one for LACP: a server bonds one link to each leaf and sees a single partner. In AVD you
do not configure MLAG at all — you put **two nodes in one node_group** of an MLAG-capable type. For `DC1_L3_LEAF1`
(dc1-leaf1a, dc1-leaf1b) AVD then generates:

| What | dc1-leaf1a | from |
|---|---|---|
| Peer-link | `interface Port-Channel3` from Ethernet3-4 | `mlag_interfaces` (via `default_interfaces[types: l3leaf].mlag_interfaces`), id = first member number |
| Peer VLAN | `vlan 4094` + `interface Vlan4094` 10.255.1.64/31, `no autostate` | `mlag_peer_vlan` 4094, `mlag_peer_ipv4_pool` |
| L3 peering VLAN | `vlan 4093` + `interface Vlan4093` 10.255.1.96/31 | `mlag_peer_l3_vlan` 4093, `mlag_peer_l3_ipv4_pool` |
| MLAG config | `domain-id DC1_L3_LEAF1`, `local-interface Vlan4094`, `peer-address 10.255.1.65`, `peer-link Port-Channel3` | node_group name, pools |
| Reload delay | `reload-delay mlag 300` / `non-mlag 330` | `platform_settings` for cEOSLab |
| Isolation | `trunk group MLAG` on 4093/4094 and the peer-link, `no spanning-tree vlan-id 4093-4094` | `trunk_groups.mlag.name` |
| Underlay iBGP | peer group `MLAG-IPv4-UNDERLAY-PEER`, neighbor 10.255.1.97, `route-map RM-MLAG-PEER-IN in` (origin incomplete) | `bgp_peer_groups.mlag_ipv4_underlay_peer`, `mlag_ibgp_origin_incomplete` |
| Per-VRF peering | `vlan 3009` (VRF10), `vlan 3010` (VRF11) reusing 10.255.1.96/31 | `mlag_ibgp_peering_vrfs.base_vlan` 3000 + vrf_id − 1 |
| Shared VTEP | Loopback1 10.255.1.3 on both, `vxlan virtual-router encapsulation mac-address mlag-system-id` | primary id |

The first node of the group (dc1-leaf1a) is the **primary** — it takes the lower address of every MLAG /31 and its id
feeds the shared VTEP address.

Dual-homed endpoints just list both switches: `dc1-leaf1-server1` has `switches: [dc1-leaf1a, dc1-leaf1b]`,
`switch_ports: [Ethernet5, Ethernet5]` and `port_channel.mode: active` → `interface Port-Channel5` with **`mlag 5`** on
both leafs. The L2 leaf below the pair gets the same treatment: `interface Port-Channel8 … mlag 8`.

:::try
id: explain-mlag
label: Explain "mlag configuration" on dc1-leaf1a
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: mlag configuration}
:::

:::try
id: explain-po5
label: Explain Port-Channel5 (the dual-homed server)
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: interface Port-Channel5}
:::

:::try
id: topology
label: See the pair and its peer-link in the Topology tab
action: open_tab
args: {tab: topology}
:::

:::check
id: domain
label: dc1-leaf1a's MLAG domain-id is its node_group name
type: config_contains
args: {node: dc1-leaf1a, pattern: "^   domain-id DC1_L3_LEAF1$"}
:::

:::check
id: peer-address
label: dc1-leaf1b points at dc1-leaf1a's Vlan4094 (10.255.1.64)
type: config_contains
args: {node: dc1-leaf1b, pattern: "^   peer-address 10\\.255\\.1\\.64$"}
:::

:::check
id: mlag-po
label: Server port-channel is an MLAG port-channel on dc1-leaf1b too
type: config_contains
args: {node: dc1-leaf1b, pattern: "^interface Port-Channel5\\n(?:   .*\\n)*?   mlag 5$"}
:::

:::quiz
id: q-vtep
question: Why do dc1-leaf1a and dc1-leaf1b have the same Loopback1 address?
options:
  - It is a bug in the lab template
  - The pair acts as one logical VTEP, so remote VTEPs can load-balance to either member and survive a member failure
  - VXLAN only supports one VTEP per rack
answer: 1
explanation: AVD computes the VTEP from the MLAG primary's id for both peers (anycast VTEP). EVPN routes from either peer carry the same next hop.
:::

**Takeaway:** in AVD, MLAG is a consequence of *two nodes in one node_group*. Pools number the peer SVIs, the group
name becomes the domain-id, and any endpoint listing both peers becomes an `mlag <id>` port-channel.
