---
id: connected-endpoints
order: 8
title: Connected endpoints & port profiles
level: core
minutes: 15
requires: [built]
tags: [endpoints, port-profiles, lacp]
summary: Describe servers, not switch ports — adapters, MLAG port-channels, access ports and reusable port profiles.
---
In AVD you describe **what is plugged in**, and AVD configures the switch ports. The lab's `servers:` list
(`group_vars/CONNECTED_ENDPOINTS.yml`, knob **Servers**) has `dc1-leaf1-server1` with two **adapters**:

```yaml
servers:
  - name: dc1-leaf1-server1
    adapters:
      - endpoint_ports: [PCI1, PCI2]
        switch_ports: [Ethernet5, Ethernet5]
        switches: [dc1-leaf1a, dc1-leaf1b]
        vlans: 11-12,21-22
        native_vlan: 4092
        mode: trunk
        spanning_tree_portfast: edge
        port_channel:
          endpoint_port_channel: Bond1
          mode: active
      - endpoint_ports: [iLO]
        switch_ports: [Ethernet5]
        switches: [dc1-leaf1c]
        vlans: 11
        mode: access
        spanning_tree_portfast: edge
```

`switches`, `switch_ports` and `endpoint_ports` are **index-aligned**: PCI1 → dc1-leaf1a Ethernet5, PCI2 → dc1-leaf1b
Ethernet5. What you get:

- **Adapter 1** — `port_channel.mode: active` bundles the ports; the channel id defaults to the number of the first
  switch port (Ethernet5 → **Port-Channel5**). Both switches are an MLAG pair, so it becomes **`mlag 5`** — one LAG
  across two switches. Members get `channel-group 5 mode active` and the description `SERVER_dc1-leaf1-server1_PCI1`
  (template `<TYPE>_<endpoint>_<endpoint_port>`); the Po gets `SERVER_dc1-leaf1-server1_Bond1`.
- **Adapter 2** — a plain access port on the L2 leaf: `switchport access vlan 11`, `spanning-tree portfast`.

If the two switches were **not** an MLAG pair you would need an `ethernet_segment` (`short_esi`) — EVPN multihoming
instead of MLAG. Endpoint types come from `connected_endpoints_keys` (servers, firewalls, routers, storage_arrays …);
the key only changes the description prefix.

**Port profiles** remove repetition: define `port_profiles: [{profile: TENANT1_TRUNK, mode: trunk, vlans: "11-12,21-22",
spanning_tree_portfast: edge}]` and set `profile: TENANT1_TRUNK` on each adapter. Precedence: adapter > profile >
parent_profile. For whole ranges of identical ports (campus access) `network_ports` applies adapter settings to
`switch_ports: [Ethernet1-48]` on switches matching a regex.

In the lab every server also becomes a linux container wired to those ports, so you can ping across the fabric.

:::try
id: open-endpoints
label: Open group_vars/CONNECTED_ENDPOINTS.yml
action: open_file
args: {path: group_vars/CONNECTED_ENDPOINTS.yml}
:::

:::try
id: explain-eth5
label: Explain dc1-leaf1a Ethernet5
action: open_file
args: {path: intended/configs/dc1-leaf1a.cfg, line: interface Ethernet5}
:::

:::try
id: clients
label: See the client containers (Clients tab)
action: open_tab
args: {tab: clients}
:::

:::check
id: bond
label: dc1-leaf1a Ethernet5 is a member of Port-Channel5
type: config_contains
args: {node: dc1-leaf1a, pattern: "^interface Ethernet5\\n(?:   .*\\n)*?   channel-group 5 mode active$"}
:::

:::check
id: ilo
label: dc1-leaf1c Ethernet5 is an access port in VLAN 11
type: config_contains
args: {node: dc1-leaf1c, pattern: "^interface Ethernet5\\n(?:   .*\\n)*?   switchport access vlan 11$"}
:::

:::quiz
id: q-esi
question: You connect a server to dc1-leaf1b and dc1-leaf2a (two different MLAG pairs) with one port-channel. What must you add?
options:
  - Nothing — AVD makes it an MLAG port-channel
  - An ethernet_segment (e.g. short_esi auto) so the two leafs use EVPN all-active multihoming
  - A second endpoint entry
answer: 1
explanation: MLAG only spans the two peers of one node_group. Across different pairs AVD needs ethernet_segment for EVPN A/A multihoming (and errors if you put one on a single-homed adapter).
:::

**Takeaway:** describe endpoints (name, adapters, switches, ports, VLANs, port-channel) and let AVD render the ports;
two MLAG peers → `mlag <id>`, other dual-homing → `ethernet_segment`, repetition → `port_profiles` / `network_ports`.
