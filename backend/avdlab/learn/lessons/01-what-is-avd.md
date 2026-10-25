---
id: what-is-avd
order: 1
title: What AVD is — inputs → structured config → CLI
level: intro
minutes: 12
requires: [built]
tags: [basics, eos_designs, eos_cli_config_gen]
summary: Follow one line of dc1-leaf1a's config back through the structured config to the group_vars that produced it.
---
Arista Validated Designs (AVD) turns a **description of your fabric** into **device configurations**. You never write
`router bgp 65101` by hand — you write *"the leafs dc1-leaf1a and dc1-leaf1b are an l3leaf pair in AS 65101"* and AVD
works out the rest. In AVD 6.4 this is a pipeline of two stages, which avdlab runs for you with **pyavd** when you press
**Build**:

1. **`eos_designs`** reads the *AVD Design* inputs — your `group_vars/*.yml` (fabric name, node types, pools, tenants,
   endpoints) — validates them against the schema and computes a **structured configuration** per device:
   `intended/structured_configs/dc1-leaf1a.yml`. This is the *EOS Config* data model, a YAML mirror of the CLI.
2. **`eos_cli_config_gen`** turns each structured config 1:1 into CLI text — `intended/configs/dc1-leaf1a.cfg` — and a
   Markdown document — `documentation/devices/dc1-leaf1a.md`.

Everything happens **offline**: no switch is needed to build. Deploying (containerlab startup-config, eAPI config
replace, or CloudVision) and validating (ANTA) are separate steps.

Take a concrete line. In the default lab, `dc1-leaf1a` has:

```
interface Loopback0
   description ROUTER_ID
   ip address 10.255.0.3/32
```

In the structured config the same thing is `loopback_interfaces: [{name: Loopback0, description: ROUTER_ID,
ip_address: 10.255.0.3/32}]`. And in the inputs? Nowhere does `10.255.0.3` appear! `group_vars/DC1_L3_LEAVES.yml`
only says `loopback_ipv4_pool: 10.255.0.0/27`, `loopback_ipv4_offset: 2` and `id: 1` for dc1-leaf1a. AVD computed
10.255.0.0/27 → address #(1 + 2) = 10.255.0.3. That is the whole idea: **you describe intent and numbering rules,
AVD derives the values** — consistently on both ends of every link.

:::try
id: open-inputs
label: Open the L3 leaf inputs (group_vars/DC1_L3_LEAVES.yml)
action: open_file
args: {path: group_vars/DC1_L3_LEAVES.yml}
:::

:::try
id: open-structured
label: Open dc1-leaf1a's structured config
action: open_file
args: {path: intended/structured_configs/dc1-leaf1a.yml}
:::

:::try
id: open-config
label: Open dc1-leaf1a's CLI and click "interface Loopback0" to explain it
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: interface Loopback0}
:::

Because eos_cli_config_gen reads **only** the structured config files, EOS Config keys you put directly into
group_vars (for example a top-level `ethernet_interfaces:`) are **ignored** after eos_designs has run — AVD 6 warns
about it. The supported ways to add raw configuration are covered in the lesson on escape hatches.

:::check
id: structured-exists
label: The build produced a structured config for dc1-leaf1a
type: file_exists
args: {path: intended/structured_configs/dc1-leaf1a.yml}
hint: Run a build first (Build tab → Build).
:::

:::check
id: loopback0-rendered
label: dc1-leaf1a's CLI contains Loopback0 10.255.0.3/32
type: config_contains
args: {node: dc1-leaf1a, pattern: "^interface Loopback0\\n(?:   .*\\n)*?   ip address 10\\.255\\.0\\.3/32"}
hint: With default pools the address is 10.255.0.3; if you changed the loopback pool, adapt the expectation.
:::

:::quiz
id: q-stages
question: You add `ethernet_interfaces:` with a description for Ethernet10 directly to group_vars/FABRIC.yml and build. What happens?
options:
  - The description appears on Ethernet10 of every device
  - Nothing — eos_cli_config_gen reads the structured config files, so top-level EOS Config keys in the design inputs are ignored (AVD warns)
  - The build fails with a schema error
answer: 1
explanation: Since AVD 6.0 eos_cli_config_gen renders from intended/structured_configs only. Use custom_structured_configuration_ethernet_interfaces or a node structured_config instead.
:::

**Takeaway:** AVD is a compiler — *design inputs* (`group_vars`) → `eos_designs` → *structured config* →
`eos_cli_config_gen` → *CLI + docs*. Read a config line backwards through that chain and you will always find the
input (and the numbering rule) that produced it.
