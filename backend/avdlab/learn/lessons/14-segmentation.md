---
id: segmentation
order: 14
title: Segmentation (MSS foundation)
level: advanced
minutes: 20
requires: [built]
tags: [mss, segmentation, security]
summary: Split VRF10 into segments and decide, per application, what may flow between them — router segment-security on the leafs.
---
A VRF separates tenants, but everything *inside* VRF10 (10.10.11.0/24 and 10.10.12.0/24) can talk freely. **Multi-domain
segmentation** adds policy inside the VRF, enforced on the leafs where the traffic enters (EOS
`router segment-security`). The lab's **Segmentation** knob is a small model on top of it:

```yaml
enabled: true
applications:
  - {name: WEB, protocol: tcp, ports: "80,443"}
policies:
  - name: P-WEB
    rules: [{application: WEB, action: forward}]
vrfs:
  - name: VRF10
    segments:
      - name: APP
        prefixes: [10.10.11.0/24]
        policies: [{from: DB, policy: P-WEB}]    # DB → APP: only web
        fallback: drop                           # anything else into APP: drop
      - name: DB
        prefixes: [10.10.12.0/24]
        fallback: forward
```

What gets rendered — only on the L3 leafs that carry VRF10 (spines and L2 leafs stay untouched), through each node's
`structured_config` (the escape hatch from lesson 10):

- `match-list input prefix-ipv4 MSS-VRF10-APP` / `MSS-VRF10-DB` — the segment prefixes;
- `application traffic recognition` — `application l4 WEB` with `field-set l4-port MSS-WEB` (80, 443);
- `router segment-security` → `policy P-WEB` (`10 application WEB action forward stateless`) and `vrf VRF10 / segment APP /
  definition / match prefix-ipv4 MSS-VRF10-APP / policies / from DB policy P-WEB / fallback policy policy-drop-all`.

Read a segment as "**traffic arriving INTO this segment** from X is judged by policy Y". A policy with no matching rule
drops. On cEOS the policies are **stateless** — the return traffic (APP → DB) is judged by DB's own policies, here
`fallback: forward`. The MSS tab shows the resulting matrix and `lab.mss_verify` tests it with real traffic between clients.

:::try
id: mss-tab
label: Open the MSS tab (policy matrix)
action: open_tab
args: {tab: mss}
:::

:::try
id: set-seg
label: Enable the example segmentation on VRF10 (then rebuild)
action: set_knob
args:
  knob: segmentation
  value:
    enabled: true
    applications: [{name: WEB, protocol: tcp, ports: "80,443"}]
    policies: [{name: P-WEB, rules: [{application: WEB, action: forward}]}]
    vrfs:
      - name: VRF10
        segments:
          - {name: APP, prefixes: [10.10.11.0/24], policies: [{from: DB, policy: P-WEB}], fallback: drop}
          - {name: DB, prefixes: [10.10.12.0/24], fallback: forward}
:::

:::try
id: verify
label: Verify the matrix with traffic (after deploy)
action: run_job
args: {job: lab.mss_verify}
:::

:::check
id: seg-enabled
label: Segmentation is enabled
type: knob_equals
args: {knob: segmentation.enabled, value: true}
:::

:::check
id: seg-rendered
label: dc1-leaf1a renders router segment-security
type: config_contains
args: {node: dc1-leaf1a, pattern: "^router segment-security$"}
hint: Rebuild after enabling segmentation.
:::

:::check
id: spine-clean
label: dc1-spine1 stays untouched (it carries no VRF)
type: config_contains
args: {node: dc1-spine1, pattern: "^router segment-security$", absent: true}
:::

:::quiz
id: q-stateless
question: With this policy, a host in APP opens SSH (tcp/22) to a host in DB. Does the session work on cEOS?
options:
  - Yes — APP → DB enters segment DB, whose fallback is forward
  - No — the SYN reaches DB (DB's fallback forwards it), but the replies DB → APP are judged by APP's `from DB` policy P-WEB, which only forwards WEB; enforcement is stateless, so the replies are dropped
  - No — the SYN is dropped by P-WEB
answer: 1
explanation: Policies are attached to the destination segment and are stateless here. Each direction needs its own permission — add SSH to a policy for `from DB` on APP (or a stateful platform) to make it work.
:::

**Takeaway:** segmentation = *segments* (prefixes per VRF) + *policies* (per application) attached to the **destination**
segment, rendered only where the VRF lives. Think in both directions — on stateless enforcement the reply path needs its
own permission.
