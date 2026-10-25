---
id: escape-hatches
order: 10
title: Custom structured configuration & raw CLI escape hatches
level: advanced
minutes: 15
requires: [built]
tags: [structured_config, custom_structured_configuration, raw_eos_cli]
summary: When the design model has no knob for it — structured_config, custom_structured_configuration_*, raw_eos_cli, and which one to pick.
---
The AVD design model covers the fabric, but sooner or later you need a line it does not generate — a banner, an
extra SNMP host, a QoS map. AVD offers escape hatches, all merged **after** eos_designs has built the structured
config, from most to least preferred:

1. **A design-model key** — always check the schema browser first (`snmp_settings`, `logging_settings`,
   `event_handlers` …). It is validated, documented and fabric-aware.
2. **`structured_config`** at a scope — a dict in the *EOS Config* model merged into that device's structured config.
   Scopes: node type (`l3leaf.defaults/node_groups/nodes[].structured_config`), platform, VRF, SVI, adapter, peer group,
   interface-level keys like `uplink_ethernet_structured_config`. Adapter/VRF-level ones are merged; **node-level ones
   are not merged across levels — only the most specific (node > node_group > defaults) is used**.
3. **`custom_structured_configuration_<eos key>`** — any group/host var, e.g.
   `custom_structured_configuration_banners: {motd: "Lab — do not touch\nEOF"}`, merged last onto the whole structured
   config. Lists with a primary key merge by key (`custom_structured_configuration_list_merge: append_rp`, default).
4. **`raw_eos_cli`** — verbatim CLI appended to the config (node, VRF, SVI, interface, platform scopes). Not validated,
   not documented, invisible to ANTA's catalog generation — use it last.

Example — add a login banner to every L3 leaf of the lab:

```yaml
# group_vars/DC1_L3_LEAVES.yml
custom_structured_configuration_banners:
  motd: |
    Managed by AVD — changes will be overwritten
    EOF
```

After a build, `intended/configs/dc1-leaf1a.cfg` contains `banner motd`. Keys inside any structured_config that are
yours (not EOS Config) must start with `_`.

The lab itself uses hatch 2: the **segmentation** and per-device **CloudVision tags** knobs write node-level
`structured_config` — which is why a hand-written node structured_config at the same level would *replace* theirs.

:::try
id: open-l3-vars
label: Open group_vars/DC1_L3_LEAVES.yml to add a custom_structured_configuration_banners key
action: open_file
args: {path: group_vars/DC1_L3_LEAVES.yml}
:::

:::try
id: rebuild
label: Rebuild the lab
action: run_job
args: {job: lab.build}
:::

:::try
id: open-leaf-cfg
label: Look for "banner motd" in dc1-leaf1a's config
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: banner motd}
:::

:::check
id: banner
label: dc1-leaf1a renders a banner motd (custom structured configuration)
type: config_contains
args: {node: dc1-leaf1a, pattern: "^banner motd$"}
hint: Add custom_structured_configuration_banners to group_vars/DC1_L3_LEAVES.yml and rebuild.
:::

:::check
id: build-after
label: The build with your change succeeded
type: job_succeeded
args: {job: lab.build}
:::

:::quiz
id: q-merge
question: l3leaf.defaults.structured_config sets a banner and node dc1-leaf1a has its own structured_config with an SNMP location. What does dc1-leaf1a get?
options:
  - Both the banner and the SNMP location
  - Only the SNMP location — node-level structured_config is not merged across defaults/node_group/node
  - Only the banner
answer: 1
explanation: For node settings only the most specific structured_config is used. Use custom_structured_configuration_* when you need additive changes.
:::

**Takeaway:** prefer the design model; when it has no knob, add EOS Config via `structured_config` or
`custom_structured_configuration_<key>` (validated, documented, merged); keep `raw_eos_cli` as the last resort.
