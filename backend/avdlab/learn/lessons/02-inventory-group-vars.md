---
id: inventory-group-vars
order: 2
title: Inventory & group_vars layering
level: intro
minutes: 12
requires: [built]
tags: [basics, inventory, group_vars]
summary: How FABRIC → DC1 → DC1_L3_LEAVES layering decides which variables a device sees.
---
AVD does not care how you split your variables into files — it only reads the **merged variables of each device**
(Ansible *hostvars*). The convention every AVD example follows, and the one the single-dc-l3ls lab uses, is a
group hierarchy in `inventory.yml`:

```text
all
 ├─ FABRIC                 fabric_name: FABRIC, credentials, pools shared by everyone, default_interfaces
 │   └─ DC1                mgmt_gateway: 172.16.1.1, management_eapi
 │       ├─ DC1_SPINES     type: spine  + spine:  {defaults, nodes}
 │       ├─ DC1_L3_LEAVES  type: l3leaf + l3leaf: {defaults, node_groups}
 │       └─ DC1_L2_LEAVES  type: l2leaf + l2leaf: {defaults, node_groups}
 ├─ NETWORK_SERVICES       children: DC1_L3_LEAVES, DC1_L2_LEAVES  → tenants: [...]
 └─ CONNECTED_ENDPOINTS    children: DC1_L3_LEAVES, DC1_L2_LEAVES  → servers: [...]
```

`group_vars/<GROUP>.yml` holds the variables of that group. A host inherits every ancestor group; **children override
parents** (DC1_L3_LEAVES beats DC1 beats FABRIC), host variables win last. So `dc1-leaf1a` sees `fabric_name` (from
FABRIC), `mgmt_gateway` (DC1), `type: l3leaf` and the `l3leaf:` block (DC1_L3_LEAVES), `tenants` (NETWORK_SERVICES) and
`servers` (CONNECTED_ENDPOINTS). The spines are not in NETWORK_SERVICES — they do not need tenants.

Two rules AVD enforces:

- **`fabric_name` is required** and must equal an inventory group containing every device of the fabric (here
  `FABRIC`). Peer facts — who is whose uplink, route server, MLAG peer — are computed across that group.
- Every device needs a **`type`** (`spine`, `l3leaf`, `l2leaf` …), usually set once per group.

:::try
id: open-inventory
label: Open inventory.yml
action: open_file
args: {path: inventory.yml}
:::

:::try
id: open-fabric
label: Open group_vars/FABRIC.yml — the fabric-wide settings
action: open_file
args: {path: group_vars/FABRIC.yml}
:::

:::try
id: open-dc1
label: Open group_vars/DC1.yml — where mgmt_gateway lives
action: open_file
args: {path: group_vars/DC1.yml}
:::

Click `ip route vrf MGMT 0.0.0.0/0 172.16.1.1` in any device config and the explainer tells you it came from
`mgmt_gateway` in `group_vars/DC1.yml` — that "where" is exactly this layering at work.

:::check
id: fabric-name
label: FABRIC.yml declares fabric_name FABRIC
type: config_contains
args: {file: group_vars/FABRIC.yml, pattern: "^fabric_name: FABRIC$"}
:::

:::check
id: dc1-gateway
label: DC1.yml sets the management gateway (mgmt_gateway)
type: config_contains
args: {file: group_vars/DC1.yml, pattern: "^mgmt_gateway: \\S+"}
:::

:::check
id: route-rendered
label: dc1-spine1 renders the management default route
type: config_contains
args: {node: dc1-spine1, pattern: "^ip route vrf MGMT 0\\.0\\.0\\.0/0 \\S+$"}
:::

:::quiz
id: q-precedence
question: "FABRIC.yml sets `p2p_uplinks_mtu: 9214` and DC1_L3_LEAVES.yml sets `p2p_uplinks_mtu: 1500`. Which MTU do the spine-side P2P interfaces get?"
options:
  - 1500 on both ends, because the leafs are the children of the links
  - 9214 on the spines — they are not in DC1_L3_LEAVES, so they only see the FABRIC value
  - The build fails because the values conflict
answer: 1
explanation: Variables are per device. Spines only inherit FABRIC → DC1 → DC1_SPINES, so they keep 9214 while the leafs use 1500 — a mismatch. Put fabric-wide settings in the group that covers every device that needs them.
:::

**Takeaway:** AVD reads *merged hostvars per device*. Put a variable in the lowest group that covers every device
needing it — FABRIC for fabric-wide, DC1 for the DC, the node-type group for type settings — and remember that a
device only sees the groups it belongs to.
