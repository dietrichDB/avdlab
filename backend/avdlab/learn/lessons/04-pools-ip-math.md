---
id: pools-ip-math
order: 4
title: Pools and the IP math (loopbacks, P2P, MLAG)
level: core
minutes: 20
requires: [built]
tags: [addressing, pools]
summary: Derive every address of dc1-leaf1b by hand from four pools and its id.
---
AVD never asks you for a per-interface address in an L3LS fabric. It asks for **pools** and computes addresses with one
helper: take the pool, cut it into subnets of a prefix length, pick subnet number *N*, then host *M*. A pool is a
comma-separated list of prefixes or ranges (`10.255.0.0/27, 10.9.9.10-10.9.9.20`).

The default lab's pools (knob **IP pools**, written to `l3leaf.defaults` / `spine.defaults`):

| pool | value | used for |
|---|---|---|
| `loopback_ipv4_pool` | 10.255.0.0/27 | Loopback0 = router-id, EVPN peering |
| `vtep_loopback_ipv4_pool` | 10.255.1.0/27 | Loopback1 = VXLAN source |
| `uplink_ipv4_pool` | 10.255.255.0/26 | /31 P2P links leaf ↔ spine |
| `mlag_peer_ipv4_pool` | 10.255.1.64/27 | Vlan4094 (MLAG control) |
| `mlag_peer_l3_ipv4_pool` | 10.255.1.96/27 | Vlan4093 + per-VRF peering SVIs |

Now do **dc1-leaf1b** (id 2, MLAG secondary of DC1_L3_LEAF1, `loopback_ipv4_offset: 2`):

- **Loopback0** = /32 #(id + offset) = #(2 + 2) → **10.255.0.4**. The spines took #1 and #2 of the same pool.
- **Loopback1 (VTEP)** — MLAG pairs share one VTEP, computed from the **primary's** id: #(1 + 2) → **10.255.1.3** on
  both dc1-leaf1a and dc1-leaf1b.
- **Uplinks** — /31 number = (id − 1) × `max_uplink_switches` × `max_parallel_uplinks` + uplink index.
  With 2 spines: (2 − 1) × 2 × 1 + 0 = **#2** → 10.255.255.4/31 → leaf **.5**, spine1 **.4**; second uplink #3 →
  leaf **.7**, spine2 **.6**. The child (leaf) takes +1, the parent (spine) +0.
- **MLAG** (`fabric_ip_addressing.mlag.algorithm: first_id`) — /31 number = primary id − 1 = 0; primary +0, secondary +1:
  Vlan4094 **10.255.1.65**, Vlan4093 **10.255.1.97**; the peer is .64 / .96.

The same pools also build `ip prefix-list PL-LOOPBACKS-EVPN-OVERLAY` (`permit 10.255.0.0/27 eq 32`, `permit
10.255.1.0/27 eq 32`), which filters what is redistributed into the underlay. If a loopback is outside those pools it
is never advertised and the overlay stays down.

:::try
id: explain-lo0
label: Explain dc1-leaf1b Loopback0
action: open_file
args: {path: intended/configs/dc1-leaf1b.cfg, line: interface Loopback0}
:::

:::try
id: open-p2p-csv
label: Compare with the fabric P2P links table
action: open_file
args: {path: documentation/fabric/FABRIC-p2p-links.csv}
:::

:::try
id: change-pool
label: Try it — move the loopback pool to 10.254.0.0/27 (then rebuild and watch every Loopback0 change)
action: set_knob
args: {knob: pools, value: {loopback: 10.254.0.0/27}}
:::

:::check
id: lo0-leaf1b
label: "dc1-leaf1b Loopback0 matches loopback_ipv4_pool #(id + offset)"
type: config_contains
args: {node: dc1-leaf1b, pattern: "^interface Loopback0\\n(?:   .*\\n)*?   ip address 10\\.25[45]\\.0\\.4/32"}
hint: 10.255.0.4 with the default pool (10.254.0.4 if you moved it). Rebuild after changing the knob.
:::

:::check
id: shared-vtep
label: dc1-leaf1a and dc1-leaf1b share the VTEP address (dc1-leaf1b shown)
type: config_contains
args: {node: dc1-leaf1b, pattern: "^interface Loopback1\\n(?:   .*\\n)*?   ip address 10\\.255\\.1\\.3/32"}
:::

:::check
id: p2p-leaf1b
label: dc1-leaf1b Ethernet1 is 10.255.255.5/31
type: config_contains
args: {node: dc1-leaf1b, pattern: "^interface Ethernet1\\n(?:   .*\\n)*?   ip address 10\\.255\\.255\\.5/31"}
:::

:::quiz
id: q-third-spine
question: You add a third spine (uplink_switches gets 3 entries, max_uplink_switches follows). What happens to dc1-leaf1b's first uplink address?
options:
  - It stays 10.255.255.5 — existing links never move
  - "It changes — the /31 number becomes (2 − 1) × 3 + 0 = #3, so every leaf's links are renumbered"
  - Only the new third link gets an address
answer: 1
explanation: max_uplink_switches is part of the formula. Set max_uplink_switches higher from day one to reserve space if you expect to add spines.
:::

:::quiz
id: q-offset
question: "Why does the l3leaf group set loopback_ipv4_offset: 2?"
options:
  - Because the spines use ids 1 and 2 in the same loopback pool
  - Because MLAG needs an offset
  - Because Loopback0 must be an even address
answer: 0
explanation: Spines and leafs share 10.255.0.0/27. Without the offset dc1-leaf1a (id 1) would get 10.255.0.1 — dc1-spine1's router-id.
:::

**Takeaway:** an address in an AVD fabric is *pool + id + offset* (P2P: *(id − 1) × max_uplink_switches + index*;
MLAG: *primary id − 1*). Size pools for your largest id, reserve room with `max_uplink_switches`, and you can derive —
and debug — every address without looking at a device.
