---
id: node-types-ids
order: 3
title: Node types, node groups and ids
level: intro
minutes: 15
requires: [built]
tags: [node_type_keys, ids, mlag]
summary: spine / l3leaf / l2leaf, defaults → node_groups → nodes inheritance, and why the id is the most important number.
---
Every device has a **`type`**. The type selects an entry of **`node_type_keys`** — a table of *behaviours*: is it an
underlay router, a VTEP, can it form MLAG, does it render network services or connected endpoints? AVD 6.4 ships the
defaults, so you only pick names:

| type | underlay router | VTEP | MLAG | services | uplinks | EVPN role |
|---|---|---|---|---|---|---|
| `spine` | yes | no | no | none | p2p | server |
| `l3leaf` | yes | **yes** | yes | L2 + L3 | p2p | client |
| `l2leaf` | **no** | no | yes | L2 | port-channel | none |

(There is no `design.type` any more — it was removed in AVD 6.0; `l3spine`/`leaf` for L2LS and `p`/`pe`/`rr` for MPLS are
just other types in the same table.)

The settings of a type live under a variable named after its key — `spine:`, `l3leaf:`, `l2leaf:` — with three
levels. The **most specific wins**: `defaults` ← `node_groups[]` ← `node_groups[].nodes[]` (or top-level `nodes[]`):

```yaml
l3leaf:
  defaults:                    # every l3leaf
    loopback_ipv4_pool: 10.255.0.0/27
    uplink_switches: [dc1-spine1, dc1-spine2]
  node_groups:
    - group: DC1_L3_LEAF1      # MLAG domain + shared settings of the pair
      bgp_as: 65101
      nodes:
        - name: dc1-leaf1a     # must equal the inventory hostname
          id: 1
          mgmt_ip: 172.16.1.101/24
        - name: dc1-leaf1b
          id: 2
```

**Node groups** are more than folders: a node_group with **exactly two** nodes of an MLAG-capable type becomes an
**MLAG pair** automatically — the first node listed is the MLAG primary. One node or three nodes → no MLAG.

The **`id`** is the index into every pool: Loopback0, VTEP, P2P uplink subnets, MLAG subnets, and the parent's downlink
port (`default_interfaces[].downlink_interfaces[id-1]`). dc1-leaf1a is id 1, so its first uplink lands on spine
Ethernet1; dc1-leaf2a is id 3 and lands on Ethernet3. Ids must be unique among nodes that share a pool — which is why
spines are 1..2 and leafs 1..4 *plus* a `loopback_ipv4_offset` of 2 (the number of spines) on the shared loopback pool.

:::try
id: open-l3
label: Open group_vars/DC1_L3_LEAVES.yml and find the ids
action: open_file
args: {path: group_vars/DC1_L3_LEAVES.yml}
:::

:::try
id: open-spine-eth3
label: Explain dc1-spine1 Ethernet3 — which leaf id put it there?
action: open_file
args: {path: intended/configs/dc1-spine1.cfg, line: interface Ethernet3}
:::

:::try
id: open-settings
label: Open the lab Settings to see leaf_pairs / l2leaf_per_pair
action: open_tab
args: {tab: settings}
:::

:::check
id: leaf2a-id
label: dc1-leaf2a is id 3 (spine1 Ethernet3 connects to it)
type: config_contains
args: {node: dc1-spine1, pattern: "^interface Ethernet3\\n   description P2P_dc1-leaf2a_Ethernet1"}
:::

:::check
id: l2leaf-no-routing
label: dc1-leaf1c (l2leaf) is not an underlay router — no `ip routing` line
type: config_contains
args: {node: dc1-leaf1c, pattern: "^ip routing$", absent: true}
:::

:::quiz
id: q-mlag
question: You add a third node dc1-leaf1e to node_group DC1_L3_LEAF1. What happens to MLAG on dc1-leaf1a/b?
options:
  - Nothing changes, the third node is ignored for MLAG
  - MLAG disappears — AVD only forms MLAG when a node_group has exactly two nodes
  - dc1-leaf1e becomes the MLAG secondary
answer: 1
explanation: MLAG is derived from node_group size (exactly 2) plus mlag_support on the type. Three nodes in a group means no MLAG at all.
:::

**Takeaway:** pick behaviour with `type`, place settings at the right level (`defaults` → `node_groups` → `nodes`),
let two-node groups become MLAG pairs, and treat each node's `id` as its seat number — every derived address and port
follows from it.
