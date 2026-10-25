---
id: bgp-underlay-overlay
order: 6
title: BGP underlay & EVPN overlay, spines as route servers
level: core
minutes: 20
requires: [built]
tags: [bgp, evpn, underlay, overlay]
summary: Two sets of BGP sessions — P2P for loopback reachability, loopback-to-loopback for EVPN — and why spines keep next-hops unchanged.
---
The single-dc-l3ls fabric runs **eBGP for everything** (`underlay_routing_protocol: ebgp`,
`overlay_routing_protocol: ebgp` in FABRIC.yml). Look at `router bgp 65101` on dc1-leaf1a — there are two families of
neighbors:

**1. Underlay — `IPv4-UNDERLAY-PEERS`.** One session per P2P link, to the spine end of each /31:
`neighbor 10.255.255.0 remote-as 65100` (dc1-spine1 Ethernet1) and `neighbor 10.255.255.2` (dc1-spine2). The only
thing exchanged is **loopbacks**: `redistribute connected route-map RM-CONN-2-BGP`, which permits
`PL-LOOPBACKS-EVPN-OVERLAY` (the loopback and VTEP pools, `eq 32`). IPv4 unicast is activated per peer group because
AVD renders `no bgp default ipv4-unicast`. Plus the MLAG peer over Vlan4093 (`MLAG-IPv4-UNDERLAY-PEER`, iBGP 65101).

**2. Overlay — `EVPN-OVERLAY-PEERS`.** Sessions between **Loopback0s**: `neighbor 10.255.0.1 remote-as 65100`
(dc1-spine1) and `10.255.0.2` (dc1-spine2), `update-source Loopback0`, `ebgp-multihop 3` (`evpn_ebgp_multihop`), `bfd`,
`send-community` (route-targets are extended communities). Only `address-family evpn` is activated on them.

**Who peers with whom?** Every node type has an `evpn_role`: spines default to **server**, l3leafs to **client**.
A client's `evpn_route_servers` defaults to its **`uplink_switches`** — so dc1-leaf1a peers with both spines. A route
server peers with every client that lists it and adds **`next-hop-unchanged`**: when dc1-spine1 relays dc1-leaf2a's
MAC route to dc1-leaf1a, the next hop stays dc1-leaf2a's VTEP 10.255.1.5, so VXLAN tunnels go leaf-to-leaf, never
through the spine.

**ASNs:** spines share 65100 (`spine.defaults.bgp_as`), each leaf pair gets its own (65101, 65102 — set on the
node_group so both MLAG peers share it). `maximum-paths 4` (`bgp_maximum_paths`) enables ECMP across both spines.

:::try
id: explain-rs
label: Explain an EVPN neighbor on dc1-spine1
action: open_file
args: {path: intended/configs/dc1-spine1.cfg, line: neighbor 10.255.0.3 peer group EVPN-OVERLAY-PEERS}
:::

:::try
id: explain-underlay
label: Explain the underlay neighbor 10.255.255.0 on dc1-leaf1a
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: neighbor 10.255.255.0 peer group IPv4-UNDERLAY-PEERS}
:::

:::try
id: fabric-doc
label: Open the fabric documentation (Loopback0 allocation table)
action: open_file
args: {path: documentation/fabric/FABRIC-documentation.md}
:::

:::check
id: nhu
label: dc1-spine1 keeps EVPN next-hops unchanged
type: config_contains
args: {node: dc1-spine1, text: "   neighbor EVPN-OVERLAY-PEERS next-hop-unchanged"}
:::

:::check
id: leaf-rs
label: dc1-leaf1a peers EVPN with dc1-spine1's Loopback0
type: config_contains
args: {node: dc1-leaf1a, pattern: "^   neighbor 10\\.255\\.0\\.1 peer group EVPN-OVERLAY-PEERS$"}
:::

:::check
id: bgp-anta
label: After deploy + validate, ANTA's BGP session checks pass on dc1-leaf1a
type: anta_status
args: {test: VerifyBGPPeerSession, device: dc1-leaf1a}
hint: Needs a deployed lab and a validation run (Validation tab).
:::

:::quiz
id: q-nhu
question: What would break if the spines rewrote the EVPN next hop to themselves?
options:
  - Nothing, it would just be slower
  - VXLAN traffic would be tunnelled to the spine, which is not a VTEP — it would be black-holed
  - The underlay sessions would flap
answer: 1
explanation: Spines in this design have no Vxlan1 interface. next-hop-unchanged keeps the originating leaf's VTEP as next hop so tunnels go leaf-to-leaf.
:::

**Takeaway:** two BGP layers — the *underlay* (P2P /31s, only loopbacks) makes every VTEP reachable; the *overlay*
(loopback-to-loopback EVPN via the spine route servers, next hop unchanged) distributes MAC/IP and prefix routes.
`evpn_role` and `uplink_switches` decide who peers with whom.
