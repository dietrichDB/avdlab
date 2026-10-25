// In-memory mock database for VITE_MOCK=1 (and tests). Shapes follow backend/avdlab/**/api.py + docs/CONTRACT.md.
import YAML from 'yaml'
import type { PsirtLast,
  AntaResult, AntaStatus, CVLastDeploy, CVOnboarding, CVTarget, Device, FileEntry, Host, HostCapabilities, Image, Job, Lab, LabStatus, Revision, Secret, Template,
  Topology, ValidateParams, ValidationRun, ValidationTotals,
} from '../api/types'

const now = () => new Date().toISOString()
const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
let seq = 1000
export const newId = () => (seq++).toString(16).padStart(8, '0') + Math.random().toString(16).slice(2, 10) + 'a'.repeat(16)

// ---------------------------------------------------------------- templates
/** CONTRACT-4: knobs every template carries (provisioning / segmentation / act) */
const PHASE4_KNOBS: Template['knobs'] = [
  { key: 'provisioning', type: 'object', label: 'Provisioning (startup-config / ZTP)', default: { mode: 'startup-config', ztp_source: 'intended', ztp_server_ip: null },
    help: 'mode: startup-config (containerlab mounts intended/configs) | ztp (cEOS boots into Zero Touch Provisioning: a `ztp` node runs dnsmasq + an HTTP server). ztp_server_ip: empty = highest free IP of the management subnet.' },
  { key: 'segmentation', type: 'yaml', label: 'Segmentation (MSS foundation)', default: {},
    help: 'router segment-security: {enabled, applications, policies, vrfs}. Rendered only on the L3 leafs that carry each VRF. Foundation — MSS design to be refined.' },
  { key: 'act', type: 'object', label: 'Arista Cloud Test (ACT)', default: { os_version: '4.33.1F', username: 'cvpadmin', password: 'cvp123!', internet_access: true, tools_server_ip: '' },
    help: 'Used when the lab target is act: the build runs pyavd in digital-twin mode (vEOS-lab, Management1) and writes the ACT topology with a tools server and one ubuntu node per client.' },
]

export const templates: Template[] = [
  {
    name: 'single-dc-l3ls',
    title: 'Single DC L3LS (EVPN/VXLAN, 2 spines, 2 MLAG leaf pairs, 2 L2 leafs)',
    description: 'AVD single-dc-l3ls example: eBGP underlay, EVPN overlay, MLAG leaf pairs with L2 leafs and two tenants.',
    fabric_name: 'FABRIC',
    generator: 'l3ls',
    knobs: [
      { key: 'mgmt_subnet', type: 'cidr', default: '172.16.1.0/24', label: 'Management subnet', help: 'containerlab mgmt network + device Management0 IPs' },
      { key: 'mgmt_gateway', type: 'ipv4', default: '172.16.1.1', label: 'Management gateway' },
      { key: 'ceos_version', type: 'string', default: '4.34.2F', label: 'cEOS version' },
      { key: 'ceos_arch', type: 'string', default: 'x86_64', choices: ['x86_64', 'arm64'], label: 'cEOS architecture', help: 'arm64 → arista/ceos:<version>-arm64 (cEOSarm); must match the host' },
      { key: 'client_image', type: 'string', default: 'ghcr.io/srl-labs/network-multitool:latest', label: 'Client image', help: 'image for connected-endpoint clients (needs iperf3, iproute2)' },
      { key: 'device_password', type: 'secret', default: 'arista', label: 'admin password' },
      { key: 'spine_count', type: 'int', default: 2, min: 1, max: 4, label: 'Spines' },
      { key: 'leaf_pairs', type: 'int', default: 2, min: 1, max: 8, label: 'MLAG leaf pairs' },
      { key: 'l2leaf_per_pair', type: 'int', default: 0, min: 0, max: 2, label: 'L2 leafs per pair' },
      { key: 'spine_asn', type: 'int', default: 65100, label: 'Spine ASN' },
      { key: 'leaf_asn_base', type: 'int', default: 65101, label: 'Leaf ASN base' },
      { key: 'serial_scheme', type: 'string', default: '{PREFIX}{ID:04d}', label: 'Serial scheme', help: 'PREFIX = upper(lab slug[:4])' },
      { key: 'pools', type: 'object', default: { loopback: '10.255.0.0/27', vtep: '10.255.1.0/27', uplink: '10.255.255.0/26', mlag_peer: '10.255.1.64/27', mlag_l3: '10.255.1.96/27' }, label: 'IP pools' },
      {
        key: 'cloudvision', type: 'object', label: 'CloudVision',
        default: { mode: 'none', region: 'auto', servers: [], disable_aaa: true, topology_hints: true, device_tags: {} },
        help: 'mode: none | cvaas | onprem. cvaas → region (auto|us-central1-a|...); onprem → servers (IPs/hostnames, port 9910). device_tags: {label: value} applied to every device.',
      },
      {
        key: 'tenants', type: 'yaml', label: 'Tenants', help: 'network_services tenants (validated by pyavd)',
        default: [{ name: 'TENANT1', mac_vrf_vni_base: 10000, vrfs: [{ name: 'VRF10', vrf_vni: 10, vtep_diagnostic: { loopback: 10, loopback_ip_range: '10.255.10.0/27' }, svis: [{ id: 11, name: 'VRF10_VLAN11', enabled: true, ip_address_virtual: '10.10.11.1/24' }, { id: 12, name: 'VRF10_VLAN12', enabled: true, ip_address_virtual: '10.10.12.1/24' }] }], l2vlans: [{ id: 3401, name: 'L2_VLAN3401' }] }],
      },
      {
        key: 'servers', type: 'yaml', label: 'Servers',
        default: [
          { name: 'dc1-host1', adapters: [{ endpoint_ports: ['eth1', 'eth2'], switch_ports: ['Ethernet5', 'Ethernet5'], switches: ['dc1-leaf1a', 'dc1-leaf1b'], vlans: '11-12', mode: 'trunk', port_channel: { mode: 'active' } }] },
          { name: 'dc1-host2', adapters: [{ endpoint_ports: ['eth1', 'eth2'], switch_ports: ['Ethernet5', 'Ethernet5'], switches: ['dc1-leaf2a', 'dc1-leaf2b'], vlans: '11-12', mode: 'trunk', port_channel: { mode: 'active' } }] },
        ],
      },
      { key: 'device_tags', type: 'yaml', default: {}, label: 'Device tags (per device)', help: 'CloudVision device tags per device: {hostname: {label: value}}; merged after the global cloudvision.device_tags (per-device wins).' },
      ...PHASE4_KNOBS,
    ],
  },
  {
    name: 'l2ls-fabric',
    title: 'L2LS fabric (2 spines MLAG, 2 leafs)',
    description: 'AVD l2ls-fabric example: layer-2 leaf-spine with MLAG spines acting as the L3 gateway.',
    fabric_name: 'FABRIC',
    generator: 'static',
    knobs: [
      { key: 'mgmt_subnet', type: 'cidr', default: '172.16.100.0/24', label: 'Management subnet' },
      { key: 'mgmt_gateway', type: 'ipv4', default: '172.16.100.1', label: 'Management gateway' },
      { key: 'device_password', type: 'secret', default: 'arista', label: 'admin password' },
      { key: 'ceos_version', type: 'string', default: '4.34.2F', label: 'cEOS version' },
      { key: 'ceos_arch', type: 'string', default: 'x86_64', choices: ['x86_64', 'arm64'], label: 'cEOS architecture' },
      { key: 'client_image', type: 'string', default: 'ghcr.io/srl-labs/network-multitool:latest', label: 'Client image' },
      { key: 'device_tags', type: 'yaml', default: {}, label: 'Device tags (per device)', help: 'CloudVision device tags per device: {hostname: {label: value}}; merged after the global cloudvision.device_tags (per-device wins).' },
      ...PHASE4_KNOBS,
    ],
  },
  {
    name: 'custom',
    title: 'Custom (imported)',
    description: 'Lab imported from an archive or git: your group_vars are the source of truth, no knobs are applied.',
    generator: 'none',
    knobs: [
      { key: 'ceos_version', type: 'string', default: '4.34.2F', label: 'cEOS version' },
      { key: 'ceos_arch', type: 'string', default: 'x86_64', choices: ['x86_64', 'arm64'], label: 'cEOS architecture' },
      { key: 'client_image', type: 'string', default: 'ghcr.io/srl-labs/network-multitool:latest', label: 'Client image' },
      { key: 'device_tags', type: 'yaml', default: {}, label: 'Device tags (per device)', help: 'CloudVision device tags per device: {hostname: {label: value}}; merged after the global cloudvision.device_tags (per-device wins).' },
      ...PHASE4_KNOBS,
    ],
  },
]

// ---------------------------------------------------------------- hosts
const caps1: HostCapabilities = {
  os: { id: 'ubuntu', name: 'Ubuntu 24.04.1 LTS', version: '24.04', family: 'debian', kernel: '6.8.0-45-generic', arch: 'x86_64' },
  cpu: { nproc: 16 }, mem: { total_kb: 65_843_200, total_gb: 62.8 }, disk: { root_free_kb: 312_000_000, docker_free_kb: 312_000_000 },
  docker: { installed: true, server_version: '27.3.1', client_version: '27.3.1', user_can_run: true, user_in_group: true },
  containerlab: { installed: true, version: '0.59.0' },
  sysctl: { 'fs.inotify.max_user_instances': 64000, 'fs.inotify.max_user_watches': 1048576, 'net.ipv4.ip_forward': 1, 'vm.max_map_count': 262144, 'fs.file-max': 2097152 },
  images: ['arista/ceos:4.34.2F', 'arista/ceos:4.33.1F'],
  remote_root: { path: '/opt/avdlab', exists: true }, python3: { installed: true, version: '3.12.3' },
  rsync: true, sudo_nopasswd: true, pipx: true, recommendations: [],
}
const caps2: HostCapabilities = {
  os: { id: 'rocky', name: 'Rocky Linux 9.4 (Blue Onyx)', version: '9.4', family: 'rhel', kernel: '5.14.0-427.el9.x86_64', arch: 'x86_64' },
  cpu: { nproc: 8 }, mem: { total_kb: 16_302_000, total_gb: 15.5 }, disk: { root_free_kb: 41_000_000, docker_free_kb: null },
  docker: { installed: true, server_version: '26.1.3', client_version: '26.1.3', user_can_run: false, user_in_group: false },
  containerlab: { installed: false, version: null },
  sysctl: { 'fs.inotify.max_user_instances': 128, 'fs.inotify.max_user_watches': 8192, 'net.ipv4.ip_forward': 0, 'vm.max_map_count': 65530, 'fs.file-max': '9223372036854775807' },
  images: [], remote_root: { path: '/opt/avdlab', exists: false }, python3: { installed: true, version: '3.9.18' },
  rsync: true, sudo_nopasswd: true, pipx: false,
  recommendations: [
    'user cannot run docker — add to the docker group (bootstrap does this) and re-login',
    'containerlab not installed — run bootstrap',
    'fs.inotify.max_user_instances=128 < 64000: too low for >10 cEOS nodes',
    'fs.inotify.max_user_watches=8192 < 1048576',
    'net.ipv4.ip_forward=0 — bootstrap sets 1',
    '15.5 GB RAM: ~1.5 GB per cEOS node → at most ~9 nodes',
    'no cEOS image present — push one from Images',
    '/opt/avdlab missing — bootstrap creates it',
  ],
}

export interface MockHost extends Host { capabilities: HostCapabilities }

function summary(c: HostCapabilities) {
  return { os: c.os?.name ?? null, docker: c.docker?.server_version ?? null, containerlab: c.containerlab?.version ?? null, recommendations: c.recommendations?.length ?? 0 }
}
export function hostOut(h: MockHost): Host {
  const { capabilities, ...rest } = h
  return { ...rest, summary: summary(capabilities) }
}

// ---------------------------------------------------------------- db
export interface MockLab extends Lab {
  files: Record<string, string>; history: (Revision & { snapshot: Record<string, string> })[]; built: boolean
  /** containerlab runtime (null = not deployed) */
  runtime?: LabStatus | null
}
export interface MockValidation extends ValidationRun { lab_id: string; results: AntaResult[] }
export interface MockJob extends Job { log: string[] }

export const db = {
  authed: false,
  secrets: [] as Secret[],
  badToken: false,
  pushChanged: {} as Record<string, string[]>,
  psirt: {} as Record<string, PsirtLast>,
  hosts: [] as MockHost[],
  images: [] as Image[],
  labs: [] as MockLab[],
  jobs: [] as MockJob[],
  validations: [] as MockValidation[],
  cvTargets: [] as CVTarget[],
}

export function reset() {
  seq = 1000
  db.authed = false
  db.secrets = [
    { id: 'sec-key-1', name: 'lab-key', kind: 'ssh_key', meta: { public_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKeyForAvdlabDevelopmentOnly avdlab', type: 'ed25519' }, created_at: ago(60 * 24 * 3) },
    { id: 'sec-pw-1', name: 'lab2-password', kind: 'password', meta: {}, created_at: ago(60 * 24) },
  ]
  db.hosts = [
    { id: 'host-lab1', name: 'lab1', kind: 'linux', address: '10.20.0.11', ssh_port: 22, ssh_user: 'ubuntu', ssh_key_secret_id: 'sec-key-1', password_secret_id: null, last_probe_at: ago(42), created_at: ago(60 * 24 * 3), summary: {}, capabilities: caps1 },
    { id: 'host-lab2', name: 'lab2', kind: 'linux', address: 'lab2.example.net', ssh_port: 2222, ssh_user: 'rocky', ssh_key_secret_id: null, password_secret_id: 'sec-pw-1', last_probe_at: ago(60 * 5), created_at: ago(60 * 24), summary: {}, capabilities: caps2 },
  ]
  db.images = [
    { id: 'img-1', fmt: 'cEOS', version: '4.34.2F', docker_tag: 'arista/ceos:4.34.2F', local_path: '/data/images/cEOS-lab-4.34.2F.tar.xz', sha256: null, size_bytes: 612_345_678, present_on_hosts: ['host-lab1'], arch: 'x86_64', created_at: ago(60 * 30) },
    { id: 'img-2', fmt: 'cEOS', version: '4.33.1F', docker_tag: 'arista/ceos:4.33.1F', local_path: null, sha256: null, size_bytes: 598_000_000, present_on_hosts: ['host-lab1'], arch: 'x86_64', created_at: ago(60 * 24 * 9) },
    { id: 'img-3', fmt: 'cEOSarm', version: '4.34.2F', docker_tag: 'arista/ceos:4.34.2F-arm64', local_path: '/data/images/cEOSarm-lab-4.34.2F.tar.xz', sha256: null, size_bytes: 571_000_000, present_on_hosts: [], arch: 'arm64', created_at: ago(60 * 5) },
  ]
  db.jobs = []
  db.pushChanged = {}
  db.psirt = {}
  db.labs = []
  db.validations = []
  db.cvTargets = [
    { id: 'cvt-cvaas', name: 'cvaas-eu', kind: 'cvaas', server: 'www.cv-prod-euwest-2.arista.io', verify_certs: true, has_token: true, has_onboarding_token: true, onboarding_token_meta: { expires_at: new Date(Date.now() + 7 * 24 * 3600_000).toISOString() }, has_ztp_script: true, ztp_script_meta: { updated_at: ago(60 * 24) }, created_at: ago(60 * 24 * 2) },
    { id: 'cvt-onprem', name: 'cvp-lab', kind: 'onprem', server: '10.20.0.50', verify_certs: false, has_token: true, has_onboarding_token: false, onboarding_token_meta: null, has_ztp_script: false, ztp_script_meta: null, created_at: ago(60 * 24 * 10) },
  ]
  const lab = createLab({ name: 'dc1-lab', template: 'single-dc-l3ls', host_id: 'host-lab1', ceos_version: '4.34.2F', knobs: {} })
  lab.created_at = ago(60 * 26)
  const j = addJob({ type: 'lab.build', lab_id: lab.id, status: 'succeeded', started: ago(60 * 2), ended: ago(60 * 2 - 0.5) })
  j.log = buildLog(lab)
  j.result = buildResult(lab)
  applyBuild(lab)
  lab.last_build_job = stripJob(j)
  seedDeployedLab()
  addJob({ type: 'host.probe', host_id: 'host-lab1', status: 'succeeded', started: ago(42), ended: ago(41.9), log: ['$ cat /etc/os-release', '[ok] probe complete'] })
  addJob({ type: 'host.bootstrap', host_id: 'host-lab2', status: 'failed', started: ago(60 * 5), ended: ago(60 * 5 - 2), error: 'step docker: re-check failed', log: ['[apply] base packages', '[ok] base packages', '[apply] docker', '[FAIL] docker: re-check failed'] })
}

// ---------------------------------------------------------------- jobs
export function stripJob(j: MockJob): Job {
  const { log, ...rest } = j
  void log
  return rest
}

export function addJob(o: { type: string; lab_id?: string; host_id?: string; status?: Job['status']; started?: string; ended?: string; error?: string; log?: string[]; params?: Record<string, unknown> }): MockJob {
  const j: MockJob = {
    id: newId(), type: o.type, status: o.status ?? 'queued', lab_id: o.lab_id ?? null, host_id: o.host_id ?? null,
    params: o.params ?? {}, result: {}, artefacts: [], error: o.error ?? null,
    created_at: o.started ?? now(), started_at: o.started ?? null, ended_at: o.ended ?? null, log: o.log ?? [],
  }
  db.jobs.unshift(j)
  return j
}

// ---------------------------------------------------------------- labs
function slugify(n: string) { return n.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') }

export function knobDefaults(t: Template): Record<string, unknown> {
  return Object.fromEntries(t.knobs.map((k) => [k.key, k.default]))
}

interface NodeDef { name: string; role: 'spine' | 'l3leaf' | 'l2leaf' | 'server'; mgmt: string; id: number; asn?: number }

export function fabricNodes(lab: MockLab): NodeDef[] {
  const k = lab.knobs ?? {}
  const base = String(k.mgmt_subnet ?? '172.16.1.0/24').split('/')[0].split('.').slice(0, 3).join('.')
  const spines = Number(k.spine_count ?? 2)
  const pairs = Number(k.leaf_pairs ?? 2)
  const l2 = Number(k.l2leaf_per_pair ?? 0)
  const out: NodeDef[] = []
  for (let i = 1; i <= spines; i++) out.push({ name: `dc1-spine${i}`, role: 'spine', mgmt: `${base}.${10 + i}`, id: i, asn: Number(k.spine_asn ?? 65100) })
  let id = 1
  for (let p = 1; p <= pairs; p++) {
    for (const s of ['a', 'b']) { out.push({ name: `dc1-leaf${p}${s}`, role: 'l3leaf', mgmt: `${base}.${100 + id}`, id, asn: Number(k.leaf_asn_base ?? 65101) + p - 1 }); id++ }
    for (let c = 0; c < l2; c++) out.push({ name: `dc1-leaf${p}${String.fromCharCode(99 + c)}`, role: 'l2leaf', mgmt: `${base}.${150 + p * 2 + c}`, id: 100 + p * 2 + c })
  }
  const servers = (Array.isArray(k.servers) ? k.servers : []) as { name: string }[]
  servers.forEach((s, i) => out.push({ name: s.name, role: 'server', mgmt: `${base}.${200 + i}`, id: 200 + i }))
  return out
}

export function renderFiles(lab: MockLab): Record<string, string> {
  const k = lab.knobs ?? {}
  const nodes = fabricNodes(lab)
  const y = (o: unknown) => '---\n' + YAML.stringify(o, { lineWidth: 0 })
  const pools = (k.pools ?? {}) as Record<string, string>
  const spines = nodes.filter((n) => n.role === 'spine')
  const leaves = nodes.filter((n) => n.role === 'l3leaf')
  const prefix = lab.slug.slice(0, 4).toUpperCase()
  const serial = (id: number) => `${prefix}${String(id).padStart(4, '0')}`
  const pairs: { group: string; nodes: NodeDef[] }[] = []
  for (let i = 0; i < leaves.length; i += 2) pairs.push({ group: `DC1_L3_LEAF${i / 2 + 1}`, nodes: leaves.slice(i, i + 2) })
  const inventory = {
    all: { children: { FABRIC: { children: { DC1: { children: {
      DC1_SPINES: { hosts: Object.fromEntries(spines.map((s) => [s.name, { ansible_host: s.mgmt }])) },
      DC1_L3_LEAVES: { children: Object.fromEntries(pairs.map((p) => [p.group, { hosts: Object.fromEntries(p.nodes.map((n) => [n.name, { ansible_host: n.mgmt }])) }])) },
    } } } }, NETWORK_SERVICES: { children: { DC1_L3_LEAVES: null } }, CONNECTED_ENDPOINTS: { children: { DC1_L3_LEAVES: null } } } },
  }
  return {
    'inventory.yml': y(inventory),
    'ansible.cfg': '[defaults]\ninventory = inventory.yml\njinja2_extensions = jinja2.ext.loopcontrols,jinja2.ext.do,jinja2.ext.i18n\n',
    'group_vars/FABRIC.yml': y({
      fabric_name: 'FABRIC',
      local_users: [{ name: 'admin', privilege: 15, role: 'network-admin', sha512_password: '$6$mock$' + btoa(String(k.device_password ?? 'arista')).slice(0, 12) }],
      mgmt_gateway: k.mgmt_gateway, mgmt_interface: 'Management0', management_eapi: { enabled: true },
      eos_designs_documentation: { topology_csv: true },
      ...cvRender(k.cloudvision),
    }),
    'group_vars/DC1.yml': y({
      spine: { defaults: { platform: 'cEOSLab', loopback_ipv4_pool: pools.loopback, bgp_as: k.spine_asn }, nodes: spines.map((s) => ({ name: s.name, id: s.id, mgmt_ip: `${s.mgmt}/24`, serial_number: serial(s.id) })) },
      l3leaf: {
        defaults: { platform: 'cEOSLab', loopback_ipv4_pool: pools.loopback, vtep_loopback_ipv4_pool: pools.vtep, uplink_interfaces: spines.map((_, i) => `Ethernet${i + 1}`), uplink_switches: spines.map((s) => s.name), uplink_ipv4_pool: pools.uplink, mlag_peer_ipv4_pool: pools.mlag_peer, mlag_peer_l3_ipv4_pool: pools.mlag_l3, mlag_interfaces: ['Ethernet3', 'Ethernet4'] },
        node_groups: pairs.map((p, i) => ({ group: p.group, bgp_as: Number(k.leaf_asn_base ?? 65101) + i, nodes: p.nodes.map((n) => ({ name: n.name, id: n.id, mgmt_ip: `${n.mgmt}/24`, serial_number: serial(n.id), uplink_switch_interfaces: spines.map(() => `Ethernet${n.id}`) })) })),
      },
    }),
    'group_vars/DC1_SPINES.yml': y({ type: 'spine' }),
    'group_vars/DC1_L3_LEAVES.yml': y({ type: 'l3leaf' }),
    'group_vars/NETWORK_SERVICES.yml': y({ tenants: k.tenants ?? [] }),
    'group_vars/CONNECTED_ENDPOINTS.yml': y({ servers: k.servers ?? [] }),
    'playbooks/build.yml': '---\n- name: Build configs\n  hosts: FABRIC\n  gather_facts: false\n  tasks:\n    - ansible.builtin.import_role:\n        name: arista.avd.eos_designs\n    - ansible.builtin.import_role:\n        name: arista.avd.eos_cli_config_gen\n',
    'playbooks/deploy.yml': '---\n- name: Deploy configs\n  hosts: FABRIC\n  gather_facts: false\n  tasks:\n    - ansible.builtin.import_role:\n        name: arista.avd.eos_config_deploy_eapi\n',
  }
}

/** CONTRACT-2c rendering of the cloudvision knob (mode none → nothing). */
export function cvRender(v: unknown): Record<string, unknown> {
  const cv = (v ?? {}) as { mode?: string; region?: string; servers?: string[]; disable_aaa?: boolean; topology_hints?: boolean; device_tags?: Record<string, string> }
  if (!cv.mode || cv.mode === 'none') return {}
  const terminattr = { disable_aaa: cv.disable_aaa ?? true }
  const cv_settings = cv.mode === 'cvaas'
    ? { cvaas: { enabled: true, clusters: [{ name: 'cvaas', region: cv.region ?? 'auto', vrf: 'use_mgmt_interface_vrf' }] }, terminattr }
    : { onprem_clusters: [{ name: 'primary', servers: (cv.servers ?? []).map((name) => ({ name })), vrf: 'use_mgmt_interface_vrf' }], terminattr }
  return { cv_settings, generate_cv_tags: { topology_hints: cv.topology_hints ?? true, device_tags: Object.entries(cv.device_tags ?? {}).map(([name, value]) => ({ name, value })) } }
}

function sha() { return Array.from({ length: 40 }, () => '0123456789abcdef'[Math.floor(Math.random() * 16)]).join('') }

export function commit(lab: MockLab, files: Record<string, string>, message: string) {
  const changed = Object.keys(files).filter((p) => lab.files[p] !== files[p])
  lab.files = { ...lab.files, ...files }
  const s = sha()
  lab.history.unshift({ sha: s, short: s.slice(0, 8), message, author: 'avdlab', date: now(), files: changed, snapshot: { ...lab.files } })
  lab.updated_at = now()
  return changed
}

export function createLab(body: { name: string; template: string; host_id?: string | null; ceos_version?: string | null; knobs: Record<string, unknown>; target?: 'clab' | 'act' }): MockLab {
  const t = templates.find((x) => x.name === body.template)!
  const lab: MockLab = {
    id: newId(), name: body.name, slug: slugify(body.name), template: body.template, host_id: body.host_id ?? null, target: body.target ?? 'clab',
    state: 'draft', ceos_version: body.ceos_version ?? null, knobs: { ...knobDefaults(t), ...body.knobs }, custom_knobs: [],
    last_build_job: null, cv_target_id: null, netbox_target_id: null, created_at: now(), updated_at: now(), files: {}, history: [], built: false,
  }
  commit(lab, renderFiles(lab), 'initial render')
  db.labs.push(lab)
  return lab
}

/** pyavd-ish validation of knob values: returns violations like the backend 422. */
export function validateLabKnobs(t: Template, knobs: Record<string, unknown>): { path: string; message: string }[] {
  const v: { path: string; message: string }[] = []
  for (const k of t.knobs) {
    const val = knobs[k.key]
    if (val === undefined) continue
    if (k.type === 'int') {
      const n = Number(val)
      if (!Number.isInteger(n)) v.push({ path: `knobs.${k.key}`, message: `${k.key}: value is not a valid integer` })
      else if (k.min != null && n < k.min) v.push({ path: `knobs.${k.key}`, message: `${k.key}: must be >= ${k.min}` })
      else if (k.max != null && n > k.max) v.push({ path: `knobs.${k.key}`, message: `${k.key}: must be <= ${k.max}` })
    }
    if (k.type === 'cidr' && !/^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/.test(String(val))) v.push({ path: `knobs.${k.key}`, message: `${k.key}: not a valid IPv4 network` })
    if (k.type === 'yaml' && k.key !== 'device_tags' && k.key !== 'segmentation' && val != null && !Array.isArray(val)) v.push({ path: `knobs.${k.key}`, message: `${k.key}: expected a list` })
  }
  const prov = knobs.provisioning as { mode?: string; ztp_server_ip?: string | null } | undefined
  if (prov && prov.mode && !['startup-config', 'ztp'].includes(prov.mode)) v.push({ path: 'knobs.provisioning.mode', message: `provisioning.mode: must be startup-config or ztp` })
  if (prov?.ztp_server_ip && !/^(\d{1,3}\.){3}\d{1,3}$/.test(prov.ztp_server_ip)) v.push({ path: 'knobs.provisioning.ztp_server_ip', message: `provisioning.ztp_server_ip: not an IPv4 address` })
  const seg = knobs.segmentation as { vrfs?: { name?: string; segments?: { name?: string; prefixes?: string[] }[] }[] } | undefined
  for (const vrf of seg?.vrfs ?? []) for (const sg of vrf.segments ?? []) for (const px of sg.prefixes ?? []) {
    if (!/^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/.test(px)) v.push({ path: `knobs.segmentation.vrfs.${vrf.name}.segments.${sg.name}.prefixes`, message: `segmentation: ${vrf.name}/${sg.name}: invalid prefix ${px}` })
  }
  const tenants = knobs.tenants
  if (Array.isArray(tenants)) {
    tenants.forEach((tn, i) => {
      const o = tn as Record<string, unknown>
      if (!o || typeof o.name !== 'string') v.push({ path: `tenants[${i}].name`, message: `tenants[${i}].name: Required key 'name' is missing` })
      if (o && o.mac_vrf_vni_base != null && typeof o.mac_vrf_vni_base !== 'number') v.push({ path: `tenants[${i}].mac_vrf_vni_base`, message: `tenants[${i}].mac_vrf_vni_base: Invalid type 'str'. Expected 'int'` })
    })
  }
  const tags = knobs.device_tags
  if (tags && typeof tags === 'object' && !Array.isArray(tags) && t.generator !== 'none') {
    const known = new Set(fabricNodes({ knobs: { ...knobDefaults(t), ...knobs }, slug: 'x' } as unknown as MockLab).filter((n) => n.role !== 'server').map((n) => n.name))
    const unknown = Object.keys(tags).filter((h) => !known.has(h)).sort()
    if (unknown.length) v.push({ path: 'device_tags', message: `device_tags: unknown hostnames [${unknown.map((u) => `'${u}'`).join(', ')}]` })
  }
  const pairs = Number(knobs.leaf_pairs ?? 0)
  const spinePorts = 8
  if (pairs * 2 > spinePorts) v.push({ path: 'knobs.leaf_pairs', message: `leaf_pairs: ${pairs * 2} leafs exceed the ${spinePorts} spine downlinks (Ethernet1-8)` })
  return v
}

export function devices(lab: MockLab): Device[] {
  const k = lab.knobs ?? {}
  const pools = (k.pools ?? {}) as Record<string, string>
  const lo = String(pools.loopback ?? '10.255.0.0/27').split('/')[0].split('.').slice(0, 3).join('.')
  const vt = String(pools.vtep ?? '10.255.1.0/27').split('/')[0].split('.').slice(0, 3).join('.')
  const prefix = lab.slug.slice(0, 4).toUpperCase()
  return fabricNodes(lab).filter((n) => n.role !== 'server').map((n) => ({
    hostname: n.name, type: n.role, platform: 'cEOSLab', mgmt_ip: `${n.mgmt}/24`, mgmt_interface: 'Management0',
    serial: `${prefix}${String(n.id).padStart(4, '0')}`,
    loopback0: `${lo}.${n.role === 'spine' ? n.id : n.id + 10}/32`,
    vtep: n.role === 'l3leaf' ? `${vt}.${Math.ceil(n.id / 2) + 2}/32` : null,
    bgp_as: n.asn ?? null, is_deployed: true, tags: deviceTags(lab, n.name),
  }))
}

/** merged global (cloudvision.device_tags, when mode != none) + per-device (device_tags knob) tags; per-device wins */
export function deviceTags(lab: MockLab, host: string): Record<string, string> {
  const cv = (lab.knobs?.cloudvision ?? {}) as { mode?: string; device_tags?: Record<string, string> }
  const per = ((lab.knobs?.device_tags ?? {}) as Record<string, Record<string, string>>)[host] ?? {}
  return { ...(cv.mode && cv.mode !== 'none' ? cv.device_tags ?? {} : {}), ...per }
}

export function topology(lab: MockLab): Topology {
  const nodes = fabricNodes(lab)
  const spines = nodes.filter((n) => n.role === 'spine')
  const leaves = nodes.filter((n) => n.role === 'l3leaf')
  const servers = nodes.filter((n) => n.role === 'server')
  const links: Topology['links'] = []
  const port = (i: string) => i.replace('Ethernet', 'eth')
  leaves.forEach((l) => spines.forEach((s, si) => {
    const a_if = `Ethernet${l.id}`, b_if = `Ethernet${si + 1}`
    links.push({ a: s.name, a_if, b: l.name, b_if, a_port: port(a_if), b_port: port(b_if), type: 'spine' })
  }))
  for (let i = 0; i < leaves.length; i += 2) {
    const [a, b] = [leaves[i], leaves[i + 1]]
    if (b) for (const e of ['Ethernet3', 'Ethernet4']) links.push({ a: a.name, a_if: e, b: b.name, b_if: e, a_port: port(e), b_port: port(e), type: 'mlag_peer' })
  }
  servers.forEach((s, i) => {
    const pair = leaves.slice(i * 2, i * 2 + 2)
    pair.forEach((l, j) => links.push({ a: l.name, a_if: 'Ethernet5', b: s.name, b_if: `eth${j + 1}`, a_port: 'eth5', b_port: `eth${j + 1}`, type: 'server' }))
  })
  const prefix = lab.slug.slice(0, 4).toUpperCase()
  return {
    lab_id: lab.id, name: lab.slug, mgmt_subnet: String(lab.knobs?.mgmt_subnet ?? ''),
    nodes: nodes.map((n) => ({
      name: n.name, kind: n.role === 'server' ? 'linux' : 'arista_ceos', role: n.role, mgmt_ip: n.mgmt,
      serial: n.role === 'server' ? null : `${prefix}${String(n.id).padStart(4, '0')}`,
      image: n.role === 'server' ? 'ghcr.io/hellt/network-multitool' : `arista/ceos:${lab.ceos_version ?? '4.34.2F'}`,
    })),
    links,
  }
}

export function buildOutputs(lab: MockLab): Record<string, string> {
  const out: Record<string, string> = {}
  for (const d of devices(lab)) {
    out[`intended/structured_configs/${d.hostname}.yml`] = `---\nhostname: ${d.hostname}\nmetadata:\n  platform: cEOSLab\n  serial_number: ${d.serial}\nmanagement_interfaces:\n  - name: Management0\n    ip_address: ${d.mgmt_ip}\nrouter_bgp:\n  as: '${d.bgp_as}'\n`
    out[`intended/configs/${d.hostname}.cfg`] = `!\nhostname ${d.hostname}\n!\ninterface Management0\n   ip address ${d.mgmt_ip}\n!\ninterface Loopback0\n   ip address ${d.loopback0}\n!\nrouter bgp ${d.bgp_as}\n   router-id ${String(d.loopback0).split('/')[0]}\n!\nend\n`
    out[`documentation/devices/${d.hostname}.md`] = `# ${d.hostname}\n\n| Mgmt | Serial |\n| --- | --- |\n| ${d.mgmt_ip} | ${d.serial} |\n`
  }
  out['documentation/fabric/FABRIC-topology.csv'] = 'Node Type,Node,Node Interface,Peer Type,Peer Node,Peer Interface\n' +
    topology(lab).links.map((l) => `x,${l.a},${l.a_if},${l.type},${l.b},${l.b_if}`).join('\n') + '\n'
  out['clab/topology.clab.yml'] = YAML.stringify({ name: lab.slug, topology: { nodes: Object.fromEntries(topology(lab).nodes.map((n) => [n.name, { kind: n.kind, 'mgmt-ipv4': n.mgmt_ip }])) } })
  return out
}

export function buildLog(lab: MockLab): string[] {
  const ds = devices(lab)
  return [
    `loading inventory + group_vars for ${lab.slug}`,
    `resolved hostvars for ${ds.length} devices`,
    'pyavd.validate_inputs …',
    ...ds.map((d) => `  [ok] ${d.hostname}: 0 violations`),
    'pyavd.get_avd_facts …',
    ...ds.flatMap((d) => [`  ${d.hostname}: structured config`, `  ${d.hostname}: eos config (${180 + d.hostname.length * 7} lines)`]),
    'get_fabric_documentation(topology_csv=True, p2p_links_csv=True)',
    'ANTA catalogs → anta/avd_catalogs',
    `containerlab topology: ${topology(lab).nodes.length} nodes, ${topology(lab).links.length} links`,
    'git commit: build',
    '[ok] build complete',
  ]
}

export function buildResult(lab: MockLab) {
  return {
    hosts: devices(lab).map((d) => d.hostname),
    warnings: Number(lab.knobs?.l2leaf_per_pair ?? 0) > 0 ? [] : ['no L2 leafs: CONNECTED_ENDPOINTS attach directly to l3leafs'],
    changed_files: Object.keys(buildOutputs(lab)).filter((p) => lab.files[p] !== buildOutputs(lab)[p]),
    fabric_name: 'FABRIC',
    anta_tests: Object.fromEntries(devices(lab).map((d) => [d.hostname, 40 + d.hostname.length])),
  }
}

export function applyBuild(lab: MockLab) {
  commit(lab, buildOutputs(lab), 'build')
  lab.built = true
  lab.state = 'built'
}

export function fileTree(lab: MockLab): FileEntry[] {
  return Object.keys(lab.files).sort().map((p) => ({
    path: p, size: lab.files[p].length,
    editable: p === 'inventory.yml' || p.startsWith('group_vars/') || p.startsWith('host_vars/') || p.startsWith('playbooks/'),
    knob_managed: p === 'inventory.yml' || p.startsWith('group_vars/'),
  }))
}

/** Tiny line diff (LCS) good enough for mock revisions. */
export function unifiedDiff(a: Record<string, string>, b: Record<string, string>): string {
  const out: string[] = []
  const paths = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()
  for (const p of paths) {
    if (a[p] === b[p]) continue
    const x = (a[p] ?? '').split('\n'), y = (b[p] ?? '').split('\n')
    out.push(`diff --git a/${p} b/${p}`, `--- ${a[p] === undefined ? '/dev/null' : `a/${p}`}`, `+++ ${b[p] === undefined ? '/dev/null' : `b/${p}`}`, `@@ -1,${x.length} +1,${y.length} @@`)
    const n = x.length, m = y.length
    const L: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
    let i = 0, j = 0
    while (i < n || j < m) {
      if (i < n && j < m && x[i] === y[j]) { out.push(' ' + x[i]); i++; j++ }
      else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { out.push('+' + y[j]); j++ }
      else { out.push('-' + x[i]); i++ }
    }
  }
  return out.join('\n')
}

// ---------------------------------------------------------------- runtime (CONTRACT-2b)
export function runtimeNodes(lab: MockLab): LabStatus['nodes'] {
  return topology(lab).nodes.map((n) => ({
    name: `clab-${lab.slug}-${n.name}`, kind: n.kind, state: 'running', mgmt_ip: n.mgmt_ip ? `${n.mgmt_ip}/24` : null,
    image: n.image ?? null, eapi: n.kind === 'arista_ceos',
  }))
}

export const DEFAULT_VALIDATE: ValidateParams = { extra_fabric_validation: true, skip_tests: ['VerifyNTP', 'VerifyReloadCause'], user_catalogs: false, tags: [] }

/**
 * A realistic anta_report.json for the lab: ~40 rows over 6 devices with the default skip list.
 * `faults` injects a broken dc1-leaf2b uplink (LLDP/BGP/reachability failures) and a timed-out `show mlag`.
 */
export function antaResults(lab: MockLab, p: ValidateParams = DEFAULT_VALIDATE, faults = false): AntaResult[] {
  const devs = devices(lab)
  const links = topology(lab).links.filter((l) => l.type === 'spine' || l.type === 'mlag_peer')
  const lo = (h: string) => String(devs.find((d) => d.hostname === h)?.loopback0 ?? '').split('/')[0]
  const spines = devs.filter((d) => d.type === 'spine')
  const bad = faults ? devs.filter((d) => d.type === 'l3leaf').slice(-1)[0]?.hostname : undefined
  const out: AntaResult[] = []
  const add = (name: string, test: string, categories: string[], description: string, result: AntaStatus, messages: string[] = [], custom_field: string | null = null) =>
    out.push({ name, test, categories, description, result, messages, custom_field })

  for (const d of devs) {
    const h = d.hostname
    const leaf = d.type !== 'spine'
    const nbrs = links.filter((l) => l.a === h || l.b === h).map((l) => (l.a === h ? { local: l.a_if, peer: l.b, peer_if: l.b_if } : { local: l.b_if, peer: l.a, peer_if: l.a_if }))
    add(h, 'VerifyNTP', ['system'], 'Verifies if NTP is synchronised.', 'failure', ['NTP status mismatch - Expected: synchronised Actual: unsynchronised'])
    add(h, 'VerifyReloadCause', ['system'], 'Verifies the last reload cause of the device.', 'failure', ['Reload cause is: Reload requested by the user.'])
    add(h, 'VerifyInterfacesStatus', ['interfaces'], 'Verifies the operational states of specified interfaces to ensure they match expected configurations.', 'success', [],
      `Interfaces: ${nbrs.map((n) => n.local).join(', ')}, Loopback0${leaf ? ', Vxlan1' : ''}`)
    const lldpBad = h === bad ? nbrs.find((n) => n.peer.includes('spine2')) : undefined
    add(h, 'VerifyLLDPNeighbors', ['connectivity'], 'Verifies the connection status of the specified LLDP (Link Layer Discovery Protocol) neighbors.',
      lldpBad ? 'failure' : 'success',
      lldpBad ? [`Port: ${lldpBad.local} Neighbor: ${lldpBad.peer} Neighbor Port: ${lldpBad.peer_if} - No LLDP neighbors`] : [],
      nbrs.map((n) => `Local: ${n.local} - Remote: ${n.peer} ${n.peer_if}`).join('; '))
    if (p.extra_fabric_validation) {
      const targets = leaf ? spines.map((s) => s.hostname) : devs.filter((x) => x.type === 'l3leaf').map((x) => x.hostname)
      add(h, 'VerifyReachability', ['connectivity'], 'Test network reachability to one or many destination IP(s).',
        h === bad ? 'failure' : 'success',
        h === bad ? [`Host: ${lo(spines[1]?.hostname ?? '')} Source: Loopback0 VRF: default - Unreachable`] : [],
        `Loopback0 → ${targets.join(', ')}`)
    }
    const peers = leaf ? spines.map((s) => s.hostname) : devs.filter((x) => x.type === 'l3leaf').map((x) => x.hostname)
    add(h, 'VerifyBGPSpecificPeers', ['bgp'], 'Verifies the health of specific BGP peer(s) for given address families.',
      h === bad ? 'failure' : 'success',
      h === bad ? [`AFI: evpn Peer: ${lo(spines[1]?.hostname ?? '')} VRF: default - Session state is not established; State: Active`] : [],
      `BGP EVPN + IPv4 peers: ${peers.join(', ')}`)
    add(h, 'VerifyRoutingTableEntry', ['routing'], 'Verifies that the provided routes are present in the routing table of a specified VRF.', 'success', [],
      `Loopback0 routes: ${peers.map(lo).join(', ')}`)
    const mlagErr = faults && h === devs.find((x) => x.type === 'l3leaf')?.hostname
    add(h, 'VerifyMlagStatus', ['mlag'], 'Verifies the health status of the MLAG configuration.',
      !leaf ? 'skipped' : mlagErr ? 'error' : 'success',
      !leaf ? ['MLAG is disabled'] : mlagErr ? ["show mlag has failed: Timed out after 30s (TimeoutException)"] : [])
    if (leaf) add(h, 'VerifyVxlan1Interface', ['vxlan'], 'Verifies the Vxlan1 interface status.', 'success', [], `Vxlan1 source Loopback1 ${String(d.vtep ?? '').split('/')[0]}`)
  }
  const skip = new Set(p.skip_tests)
  return out.filter((r) => !skip.has(r.test))
}

export function totalsOf(rows: AntaResult[]): ValidationTotals {
  const t: ValidationTotals = { success: 0, failure: 0, error: 0, skipped: 0, unset: 0 }
  for (const r of rows) t[r.result]++
  return t
}

export function antaMarkdown(rows: AntaResult[]): string {
  const t = totalsOf(rows)
  const devs = [...new Set(rows.map((r) => r.name))]
  const esc = (x: string) => x.replace(/\|/g, '\\|').replace(/\n/g, '<br>')
  const lines = [
    '# ANTA Report', '', '**Table of Contents:**', '', '- [ANTA Report](#anta-report)', '  - [Test Results Summary](#test-results-summary)',
    '    - [Summary Totals](#summary-totals)', '    - [Summary Totals Device Under Test](#summary-totals-device-under-test)', '  - [Test Results](#test-results)', '',
    '## Test Results Summary', '', '### Summary Totals', '',
    '| Total Tests | Total Tests Success | Total Tests Skipped | Total Tests Failure | Total Tests Error |',
    '| ----------- | ------------------- | ------------------- | ------------------- | ------------------|',
    `| ${rows.length} | ${t.success} | ${t.skipped} | ${t.failure} | ${t.error} |`, '',
    '### Summary Totals Device Under Test', '',
    '| Device Under Test | Total Tests | Tests Success | Tests Skipped | Tests Failure | Tests Error | Categories Skipped | Categories Failed |',
    '| ------------------| ----------- | ------------- | ------------- | ------------- | ----------- | -------------------| ------------------|',
    ...devs.map((d) => {
      const rs = rows.filter((r) => r.name === d), dt = totalsOf(rs)
      const cats = (s: AntaStatus[]) => [...new Set(rs.filter((r) => s.includes(r.result)).flatMap((r) => r.categories))].join(', ') || '-'
      return `| ${d} | ${rs.length} | ${dt.success} | ${dt.skipped} | ${dt.failure} | ${dt.error} | ${cats(['skipped'])} | ${cats(['failure', 'error'])} |`
    }),
    '', '## Test Results', '',
    '| Device Under Test | Test Name | Test Status | Message(s) | Test description | Test category |',
    '| ----------------- | --------- | ----------- | ---------- | ---------------- | ------------- |',
    ...rows.map((r) => `| ${r.name} | ${r.test} | ${r.result} | ${esc(r.messages.join('\n')) || '-'} | ${esc(r.description)} | ${r.categories.join(', ')} |`),
    '',
  ]
  return lines.join('\n')
}

export function antaCsv(rows: AntaResult[]): string {
  const q = (x: string) => (/[",\n]/.test(x) ? `"${x.replace(/"/g, '""')}"` : x)
  return ['Device,Test Name,Test Status,Message(s),Test description,Test category',
    ...rows.map((r) => [r.name, r.test, r.result, r.messages.join(' - '), r.description, r.categories.join(', ')].map(q).join(','))].join('\n') + '\n'
}

export function addValidation(lab: MockLab, results: AntaResult[], job_id: string | null, created_at = now()): MockValidation {
  const v: MockValidation = { id: newId(), lab_id: lab.id, job_id, created_at, totals: totalsOf(results), report_path: `anta/reports/anta_report.json`, results }
  db.validations.unshift(v)
  return v
}

/** CONTRACT-4 C: a small segmentation example (VLAN11 = web, VLAN12 = app in VRF10) */
export const MSS_SEED = {
  enabled: true,
  applications: [{ name: 'web', protocol: 'tcp', ports: '80,443' }, { name: 'ssh', protocol: 'tcp', ports: '22' }, { name: 'ping', protocol: 'icmp', ports: null }],
  policies: [
    { name: 'web-only', rules: [{ application: 'web', action: 'forward', log: false }, { application: 'ping', action: 'forward', log: false }] },
    { name: 'deny-ssh', rules: [{ application: 'ssh', action: 'drop', log: true }, { application: 'app-match-all', action: 'forward', log: false }] },
  ],
  vrfs: [{ name: 'VRF10', segments: [
    { name: 'web', prefixes: ['10.10.11.0/24'], interfaces: [], policies: [{ from: 'app', policy: 'deny-ssh' }], fallback: 'drop' },
    { name: 'app', prefixes: ['10.10.12.0/24'], interfaces: [], policies: [{ from: 'web', policy: 'web-only' }], fallback: 'drop' },
  ] }],
}

function seedDeployedLab() {
  const lab = createLab({ name: 'evpn-lab', template: 'single-dc-l3ls', host_id: 'host-lab1', ceos_version: '4.34.2F', knobs: { mgmt_subnet: '172.16.20.0/24', mgmt_gateway: '172.16.20.1', cloudvision: { mode: 'cvaas', region: 'euwest-2', servers: [], disable_aaa: true, topology_hints: true, device_tags: { lab: 'evpn-lab', owner: 'netops' } }, servers: [
    ...(templates[0].knobs.find((k) => k.key === 'servers')!.default as unknown[]),
    { name: 'dc1-host3', adapters: [{ endpoint_ports: ['eth1'], switch_ports: ['Ethernet6'], switches: ['dc1-leaf2a'], vlans: '12', mode: 'access' }] },
  ], segmentation: MSS_SEED } })
  lab.cv_target_id = 'cvt-cvaas'
  lab.created_at = ago(60 * 6)
  const b = addJob({ type: 'lab.build', lab_id: lab.id, status: 'succeeded', started: ago(60 * 5), ended: ago(60 * 5 - 0.4) })
  b.log = buildLog(lab); b.result = buildResult(lab)
  applyBuild(lab)
  lab.last_build_job = stripJob(b)
  addJob({ type: 'lab.deploy', lab_id: lab.id, host_id: lab.host_id!, status: 'succeeded', started: ago(60 * 4), ended: ago(60 * 4 - 3.2), params: { reconfigure: true, wait_eapi: true, wait_timeout: 600 }, log: ['[ok] deploy'] })
  const nodes = runtimeNodes(lab)
  const flaky = nodes.find((n) => n.name.endsWith('dc1-leaf2b'))
  if (flaky) flaky.eapi = false
  lab.runtime = { deployed: true, nodes }
  lab.state = 'deployed'
  lab.settings = { ...(lab.settings ?? {}), deployed_at: ago(60 * 4 - 3.2), cv_onboarding: cvOnboardingOf(lab, ago(50), ['dc1-leaf2b']) }
  lab.settings.cv_last_deploy = cvDeployResultOf(lab, { submit_workspace: true, run_change_control: false }, ago(45), ['dc1-leaf2b'])
  const v = addJob({ type: 'lab.validate', lab_id: lab.id, host_id: lab.host_id!, status: 'succeeded', started: ago(30), ended: ago(29), params: { ...DEFAULT_VALIDATE }, log: ['[ok] anta'] })
  const results = antaResults(lab, DEFAULT_VALIDATE, true)
  v.result = { totals: totalsOf(results) }
  addValidation(lab, results, v.id, ago(29))
}

// ---------------------------------------------------------------- CloudVision (CONTRACT-2c)
export function cvOnboardingOf(lab: MockLab, checked_at = now(), notStreaming: string[] = []): CVOnboarding {
  return {
    checked_at,
    nodes: devices(lab).map((d) => {
      const down = notStreaming.includes(d.hostname)
      return { name: `clab-${lab.slug}-${d.hostname}`, serial: d.serial ?? null, terminattr_running: true, streaming: !down }
    }),
  }
}

export function cvDeployResultOf(lab: MockLab, p: { submit_workspace: boolean; run_change_control: boolean; workspace_name?: string }, finished_at = now(), missing: string[] = []): CVLastDeploy {
  const wsId = `ws-${lab.slug}-${Math.random().toString(16).slice(2, 10)}`
  const target = db.cvTargets.find((t) => t.id === lab.cv_target_id)
  const devs = devices(lab).map((d) => ({ hostname: d.hostname, serial: d.serial ?? null, exists_on_cv: !missing.includes(d.hostname), streaming: !missing.includes(d.hostname) }))
  return {
    workspace: { id: wsId, state: p.submit_workspace ? 'submitted' : 'pending', url: target ? `https://${target.server}/cv/provisioning/workspaces?ws=${wsId}` : null },
    change_control: p.submit_workspace && p.run_change_control ? { id: `cc-${wsId.slice(3)}`, state: 'completed' } : p.submit_workspace ? { id: `cc-${wsId.slice(3)}`, state: 'pending approval' } : null,
    devices: devs,
    errors: [],
    warnings: devs.filter((d) => !d.exists_on_cv).map((d) => `device ${d.hostname} (serial ${d.serial}) not found on CloudVision — skipped (skip_missing_devices)`),
    finished_at,
  }
}

reset()

// ---------------------------------------------------------------- v0.8.0 (CONTRACT-7 A): users, roles, MFA
import type { AuditEntry as MockAuditEntry, UserAccount as MockUserAccount } from '../api/types'

/** A mock account: `password` null = any password is accepted (keeps the mock UI easy to sign into). */
export interface MockUser extends MockUserAccount { password: string | null; totp_code: string; recovery: string[] }

export const MOCK_TOTP = '123456'

export const authDb = {
  users: [] as MockUser[],
  audit: [] as MockAuditEntry[],
  currentId: null as string | null,
  pendingId: null as string | null,
  mfaAttempts: 0,
  setupSecret: null as string | null,
}

function mockUser(id: string, username: string, role: MockUser['role'], extra: Partial<MockUser> = {}): MockUser {
  return {
    id, username, role, disabled: false, must_change_password: false, mfa_enabled: false, recovery_codes_left: 0,
    failed_logins: 0, locked: false, locked_until: null, last_login_at: ago(60 * 3), password_changed_at: ago(60 * 24 * 30),
    created_at: ago(60 * 24 * 60), password: null, totp_code: MOCK_TOTP, recovery: [], ...extra,
  }
}

export function resetAuth() {
  authDb.users = [
    mockUser('usr-admin', 'admin', 'admin'),
    mockUser('usr-op', 'olivia', 'operator', { password: 'Operator-Pass-1' }),
    mockUser('usr-mfa', 'mfa-user', 'operator', { password: 'Secret-Pass-123', mfa_enabled: true, recovery: ['abcde-fghij', 'klmno-pqrst'], recovery_codes_left: 2 }),
    mockUser('usr-new', 'newbie', 'viewer', { password: 'Temp-Pass-0001', must_change_password: true, last_login_at: null }),
    mockUser('usr-locked', 'locked', 'viewer', { locked: true, locked_until: new Date(Date.now() + 15 * 60_000).toISOString(), failed_logins: 5 }),
  ]
  authDb.audit = [
    { id: 2, at: ago(5), username: 'locked', event: 'login.lockout', ip: '10.0.0.7', user_agent: 'Mozilla/5.0', detail: '5 failed attempts' },
    { id: 1, at: ago(60 * 3), username: 'admin', event: 'login.ok', ip: '10.0.0.5', user_agent: 'Mozilla/5.0', detail: null },
  ]
  authDb.currentId = null
  authDb.pendingId = null
  authDb.mfaAttempts = 0
  authDb.setupSecret = null
}
resetAuth()

// ---------------------------------------------------------------- v0.9.0 (CONTRACT-8 A): account requests + admin settings
import type { AccountRequest as MockAccountRequest, AppSettingsValues as MockAppSettingsValues } from '../api/types'

export const SETTINGS_DEFAULTS: MockAppSettingsValues = {
  app_title: 'avdlab', github_url: 'https://github.com/example/avdlab', support_email: 'avdlab-support@example.com',
  registration_enabled: true, intro_video_url: '/intro/avdlab-intro.mp4', feedback_mail_to: 'dietrich@arista.com',
  feedback_mail_enabled: true, registration_mail_to: '', smtp_host: '', smtp_port: 587, smtp_user: '', smtp_starttls: true,
  smtp_from: '',
}

export const regDb = {
  requests: [] as MockAccountRequest[],
  settings: { ...SETTINGS_DEFAULTS } as MockAppSettingsValues,
  overridden: [] as string[],
  smtpPassword: '',
  mails: [] as { to: string; subject: string }[],
}

export function resetRegistration() {
  regDb.requests = [
    { id: 'req-ada', username: 'ada', email: 'ada@example.com', full_name: 'Ada Lovelace', reason: 'Learning AVD for a customer PoC', status: 'pending', ip: '10.0.0.42', role: null, user_id: null, decided_by: null, decided_at: null, decision_reason: null, created_at: ago(90) },
  ]
  regDb.settings = { ...SETTINGS_DEFAULTS }
  regDb.overridden = []
  regDb.smtpPassword = ''
  regDb.mails = []
}
resetRegistration()
