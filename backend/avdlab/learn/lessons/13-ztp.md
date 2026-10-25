---
id: ztp
order: 13
title: Zero Touch Provisioning (ZTP)
level: advanced
minutes: 15
requires: [built]
tags: [ztp, dhcp, provisioning]
summary: Boot switches with no config and let DHCP option 67 hand them their AVD config — the way real racks are brought up.
---
So far the lab handed each cEOS its intended config as **startup-config** (containerlab mounts
`intended/configs/<node>.cfg`). Real switches arrive empty. EOS then runs **Zero Touch Provisioning**:

1. No startup-config → ZTP starts and sends **DHCP** requests on the management interface.
2. The DHCP server answers with an address and **option 67** (bootfile URL).
3. EOS downloads the bootfile over HTTP. A file starting with `#!` is **executed** as a script (used for CloudVision
   bootstrap); anything else is applied as the **startup-config**, then the switch reboots/loads it.
4. Any saved startup-config cancels ZTP (`zerotouch cancel` does too).

Set the **Provisioning** knob to `mode: ztp` and the lab changes shape (AVD's configs do not change at all):

- the switches get **no** startup-config;
- a linux node **`ztp`** runs `dnsmasq` + a tiny HTTP server; the build writes `clab/ztp/dnsmasq.conf`, `clab/ztp/run.sh`
  and `clab/ztp/www/<node>.cfg` (the intended config of each switch);
- at deploy time the switches' MAC addresses are learned and a `dhcp-host` entry per switch pins its **AVD `mgmt_ip`**
  and its option 67 — `http://<ztp server>/dc1-leaf1a.cfg`;
- with `ztp_source: cloudvision` the bootfile is instead a bash script (`bootstrap-<node>.sh`) that runs the CloudVision
  ZTP bootstrap / writes the onboarding token, so the switch registers in CloudVision and gets its config from there.

ANTA's `VerifyZeroTouch` test (in every AVD catalog) checks that ZTP is **disabled** once a switch is provisioned —
a switch still in ZTP mode would wipe itself on the next reload.

:::try
id: set-ztp
label: Switch the lab to ZTP provisioning (then rebuild)
action: set_knob
args: {knob: provisioning, value: {mode: ztp, ztp_source: intended}}
:::

:::try
id: open-dnsmasq
label: Open the generated dnsmasq skeleton
action: open_file
args: {path: clab/ztp/dnsmasq.conf}
:::

:::try
id: runtime
label: Watch leases and downloads in the Runtime tab after deploying
action: open_tab
args: {tab: runtime}
:::

:::check
id: ztp-mode
label: Provisioning mode is ztp
type: knob_equals
args: {knob: provisioning.mode, value: ztp}
:::

:::check
id: bootfile
label: The ZTP server has a bootfile for dc1-leaf1a
type: file_exists
args: {path: clab/ztp/www/dc1-leaf1a.cfg}
hint: Rebuild (or regenerate the topology) after switching to ztp mode.
:::

:::quiz
id: q-hashbang
question: The bootfile served for dc1-leaf1a starts with `#!/bin/bash`. What does EOS do with it?
options:
  - Applies it as startup-config
  - Executes it as a script (e.g. the CloudVision bootstrap), which then provisions the switch
  - Ignores it and retries DHCP
answer: 1
explanation: EOS ZTP executes bootfiles that start with a shebang and treats anything else as a startup-config.
:::

**Takeaway:** ZTP = DHCP (address + option 67) + HTTP (bootfile). The lab reproduces it with a dnsmasq/HTTP container
that serves AVD's intended configs — same configs, realistic day-0 path — and ANTA's VerifyZeroTouch guards the day-2 state.
