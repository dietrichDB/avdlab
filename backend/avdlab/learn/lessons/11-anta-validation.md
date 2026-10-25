---
id: anta-validation
order: 11
title: "ANTA: how validation works and reading a report"
level: core
minutes: 20
requires: [built, deployed]
tags: [anta, validation, testing]
summary: AVD writes per-device ANTA catalogs from the structured configs; ANTA runs them over eAPI and reports success / failure / error / skipped.
---
Building tells you what the network **should** be; **ANTA** (Arista Network Test Automation, 1.10 in this lab) checks
what it **is**. AVD bridges the two: during the build it generates a **catalog per device** from the structured config —
`anta/avd_catalogs/dc1-leaf1a.json` — containing only tests that make sense for that device, with expected values
filled in: `VerifyBGPPeerSession` with dc1-leaf1a's real peers (10.255.255.0, 10.255.255.2, 10.255.0.1 …),
`VerifyLLDPNeighbors` with the expected neighbor on every link (Ethernet1 → dc1-spine1 Ethernet1),
`VerifyMlagStatus`, `VerifyVxlanConfigSanity`, `VerifyRoutingProtocolModel` (multi-agent), `VerifyInterfacesStatus`,
`VerifyReachability` over each P2P /31 …

**Validate** (Validation tab) runs ANTA against the deployed lab over eAPI (the `arista` user, HTTPS on the management
IPs). Each test yields one of:

| status | meaning |
|---|---|
| **success** | the device matches the intent |
| **failure** | the test ran and the state is wrong (BGP peer not Established, LLDP neighbor missing …) |
| **error** | the test could not run (command failed, timeout, unreachable device) |
| **skipped** | not applicable (e.g. hardware tests on cEOSLab — AVD does not even generate them for virtual platforms) |

The lab skips `VerifyNTP` and `VerifyReloadCause` by default (`avd_catalogs_filters` — containers rarely have a usable
NTP source or a clean reload cause) and enables `avd_catalogs_extra_fabric_validation` (fabric-wide reachability, e.g.
VTEP to VTEP). Reports are JSON / Markdown / CSV per run.

How to read a failing report: sort by **device**, then by **category**. A failing `VerifyLLDPNeighbors` usually means a
cabling (topology) mistake; `VerifyBGPPeerSession` failing on the overlay but not the underlay points at loopback
reachability (`PL-LOOPBACKS-EVPN-OVERLAY`, pools); MLAG failures at the peer-link or node_group; errors on one device
at its reachability or credentials.

:::try
id: catalog
label: Open the AVD-generated catalog of dc1-leaf1a
action: open_file
args: {path: anta/avd_catalogs/dc1-leaf1a.json}
:::

:::try
id: run-validate
label: Run a validation
action: run_job
args: {job: lab.validate}
:::

:::try
id: open-validation
label: Open the Validation tab to read the report
action: open_tab
args: {tab: validation}
:::

:::check
id: catalog-exists
label: AVD generated ANTA catalogs
type: file_exists
args: {path: "anta/avd_catalogs/*.json"}
:::

:::check
id: validated
label: The last validation job succeeded
type: job_succeeded
args: {job: lab.validate}
hint: Deploy the lab first (it needs running switches).
:::

:::check
id: clean-report
label: The last report has no failures or errors
type: anta_status
args: {max_failure: 0, min_success: 1}
:::

:::quiz
id: q-status
question: dc1-leaf2b was powered off during the run. Which status do its tests most likely show?
options:
  - failure
  - error — the tests could not collect any output
  - skipped
answer: 1
explanation: failure means the test ran and found a wrong state; an unreachable device cannot run its commands, which ANTA reports as error.
:::

:::quiz
id: q-catalog
question: Where do the expected values in the AVD-generated catalogs come from?
options:
  - From the running switches
  - From the structured configs of the build (intent), not from raw_eos_cli or the device
  - From the ANTA defaults
answer: 1
explanation: AVD builds catalogs from intended/structured_configs — which is why raw_eos_cli additions are invisible to them.
:::

**Takeaway:** build = intent, ANTA = reality. AVD turns the intent into per-device catalogs; validation compares; read
reports by device and category, and map failures back to the input that should have produced the missing state.
