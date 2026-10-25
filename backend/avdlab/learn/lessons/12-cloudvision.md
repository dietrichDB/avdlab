---
id: cloudvision
order: 12
title: CloudVision onboarding & cv_deploy
level: advanced
minutes: 20
requires: [built]
tags: [cloudvision, terminattr, cv_deploy]
summary: cv_settings → TerminAttr streaming, onboarding tokens, device tags, and how cv_deploy pushes configs through workspaces and change controls.
---
CloudVision (CVaaS or on-prem) needs two things from each switch: **streaming** (TerminAttr) and a way to **push
configuration** (Static Configuration Studio via `cv_deploy`).

**Streaming.** The lab's **CloudVision** knob (`mode: none | cvaas | onprem`) writes AVD's `cv_settings` into the fabric
group. For CVaaS with region auto you get on every switch:

```
daemon TerminAttr
   exec /usr/bin/TerminAttr -cvaddr=apiserver.arista.io:443 -cvauth=token-secure,/tmp/cv-onboarding-token -cvvrf=MGMT -disableaaa -smashexcludes=… -taillogs -cvsourceintf=Management1
   no shutdown
```

`cv_settings` replaced the 5.x keys (`cvp_instance_ips`, `terminattr_*`). AVD refuses CloudVision without
`ntp_settings.servers` (certificates need correct time) and CVaaS without DNS — the lab adds pool.ntp.org and public
resolvers if your template has none. The **onboarding token** is never in the config: the onboarding job writes it to
`/tmp/cv-onboarding-token` (CVaaS) or `/tmp/token` (on-prem) on each switch.

**Tags.** `generate_cv_tags.topology_hints` adds topology tags (role, rack …) so CloudVision draws the fabric; the
**device tags** knobs add your own labels, rendered into `metadata.cv_tags` of the structured config.

**cv_deploy** (replaces the removed `eos_config_deploy_cvp` role) identifies devices by **serial number** (then system MAC,
then hostname) — which is why the lab gives every cEOS a deterministic serial (`serial_scheme`). It then: creates a
**workspace**, uploads one configlet per device (`AVD-<hostname>`) with the full intended config, assigns tags, builds
and submits the workspace, and creates a **change control** (left *pending approval* unless `cv_run_change_control`).

:::try
id: cv-tab
label: Open the CloudVision tab (targets, onboarding, deploy)
action: open_tab
args: {tab: cloudvision}
:::

:::try
id: set-cvaas
label: Switch the lab to CVaaS streaming (then rebuild)
action: set_knob
args: {knob: cloudvision, value: {mode: cvaas, region: auto}}
:::

:::try
id: explain-ta
label: Explain the TerminAttr line on dc1-spine1
action: open_file
args: {path: intended/configs/dc1-spine1.cfg, line: daemon TerminAttr}
:::

:::check
id: cv-mode
label: The lab streams to CVaaS (cloudvision.mode is cvaas)
type: knob_equals
args: {knob: cloudvision.mode, value: cvaas}
hint: Set the CloudVision knob to mode cvaas in Settings (an on-prem lab can skip this checkpoint).
:::

:::check
id: terminattr
label: dc1-spine1 renders daemon TerminAttr
type: config_contains
args: {node: dc1-spine1, pattern: "^daemon TerminAttr$"}
hint: Rebuild after changing the CloudVision knob.
:::

:::check
id: ntp
label: NTP is configured (required by cv_settings)
type: config_contains
args: {node: dc1-spine1, pattern: "^ntp server vrf MGMT \\S+"}
:::

:::quiz
id: q-serial
question: You rebuild the lab with a different serial_scheme after onboarding it to CVaaS. What does cv_deploy see?
options:
  - The same devices — it matches by hostname
  - New devices — cv_deploy matches by serial number first, so the old devices look missing and the new serials unknown
  - Nothing changes because serials are only documentation
answer: 1
explanation: Serial numbers are the primary device key for cv_deploy and CloudVision inventory. Keep serials stable for onboarded labs.
:::

**Takeaway:** `cv_settings` makes switches *stream* (TerminAttr, needs NTP/DNS and an out-of-band token), tags make the
inventory meaningful, and `cv_deploy` pushes AVD's intended configs through workspaces and change controls — keyed on
stable serial numbers.
