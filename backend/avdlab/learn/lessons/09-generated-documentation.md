---
id: generated-documentation
order: 9
title: Reading the generated documentation
level: intro
minutes: 10
requires: [built]
tags: [documentation]
summary: Device docs, fabric docs, topology and P2P CSVs — the free, always-up-to-date documentation of your lab.
---
Every build also writes documentation — generated from the same structured config as the CLI, so it can never drift:

- **`documentation/devices/<host>.md`** (eos_cli_config_gen) — one page per device: management (interfaces, DNS, NTP,
  eAPI), authentication, MLAG summary, spanning tree, VLANs, interfaces (summary tables + the device configuration
  snippet), routing (BGP neighbors, VRFs), prefix-lists and route-maps. Passwords are hidden by default
  (`eos_cli_config_gen_documentation.hide_passwords: true`).
- **`documentation/fabric/FABRIC-documentation.md`** (eos_designs) — the fabric view: all switches with mgmt IPs and
  serials, the **topology** table (who connects to whom), **P2P link allocation**, **Loopback0** and **VTEP** allocation
  per node. This is where you check the IP math from the pools lesson in one place.
- **`FABRIC-topology.csv`** and **`FABRIC-p2p-links.csv`** — enabled with `eos_designs_documentation.topology_csv` /
  `p2p_links_csv` in FABRIC.yml. avdlab builds the containerlab topology from the topology CSV (every physical link
  AVD knows about becomes a veth pair).

A good habit: after every input change, **diff the documentation** (Build tab → diff) — a changed Loopback0 table or a
new P2P row tells you the blast radius before you deploy.

:::try
id: device-doc
label: Open dc1-leaf1a's device documentation
action: open_file
args: {path: documentation/devices/dc1-leaf1a.md}
:::

:::try
id: fabric-doc
label: Open the fabric documentation
action: open_file
args: {path: documentation/fabric/FABRIC-documentation.md}
:::

:::try
id: topo-csv
label: Open FABRIC-topology.csv (source of the containerlab links)
action: open_file
args: {path: documentation/fabric/FABRIC-topology.csv}
:::

:::check
id: fabric-doc-exists
label: The fabric documentation exists
type: file_exists
args: {path: documentation/fabric/FABRIC-documentation.md}
:::

:::check
id: p2p-row
label: The P2P CSV lists dc1-leaf1a Ethernet1 ↔ dc1-spine1
type: config_contains
args: {file: documentation/fabric/FABRIC-p2p-links.csv, pattern: "dc1-leaf1a,[^,]*,Ethernet1,10\\.255\\.255\\.1/31,spine,dc1-spine1"}
:::

:::check
id: build-ok
label: The last build succeeded
type: job_succeeded
args: {job: lab.build}
:::

:::quiz
id: q-doc
question: Where do you check, in one table, which Loopback0 every switch got?
options:
  - documentation/devices/dc1-spine1.md
  - documentation/fabric/FABRIC-documentation.md (Loopback0 Interfaces Node Allocation)
  - anta/avd_catalogs
answer: 1
explanation: The fabric document lists the loopback pools and a per-node allocation table; device docs only show their own device.
:::

**Takeaway:** the documentation is a build artefact, not a chore — device docs explain one switch, the fabric doc and
CSVs explain the fabric, and diffing them is the cheapest change review you will ever do.
