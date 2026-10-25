// Response types from docs/CONTRACT.md and backend/avdlab/api/*.py

export type HostKind = 'linux' | 'act'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type LabState = 'draft' | 'built' | 'deployed' | 'validated' | 'destroyed'
export type SecretKind = 'ssh_key' | 'password' | 'arista_token' | 'cv_token' | 'netbox_token' | 'generic'

// ---- auth / secrets (given routers)
export interface Me {
  user: string
}

export interface Secret {
  id: string
  name: string
  kind: SecretKind
  meta: Record<string, unknown>
  created_at: string
}

// ---- jobs (backend/avdlab/api/jobs.py JobOut)
export interface Job {
  id: string
  type: string
  status: JobStatus
  lab_id: string | null
  host_id: string | null
  params: Record<string, unknown>
  result: Record<string, unknown>
  artefacts: { name: string; path: string }[]
  error: string | null
  created_at: string
  started_at: string | null
  ended_at: string | null
}

export interface JobRef {
  job_id: string
}

export type JobStreamFrame = { line: string } | { done: true }

// ---- hosts (backend/avdlab/hosts/api.py HostOut + hosts/probe.py capabilities)
export interface HostCapabilities {
  os?: { id?: string | null; name?: string | null; version?: string | null; family?: string | null; kernel?: string | null; arch?: string | null
    /** 1.0: linux | wsl | orbstack — a Linux VM on Windows/macOS is a beta lab host */
    platform?: 'linux' | 'wsl' | 'orbstack' | string | null; platform_label?: string | null; beta?: boolean; docker_desktop?: boolean }
  cpu?: { nproc?: number | null }
  mem?: { total_kb?: number | null; total_gb?: number | null }
  disk?: { root_free_kb?: number | null; docker_free_kb?: number | null }
  docker?: { installed?: boolean; server_version?: string | null; client_version?: string | null; user_can_run?: boolean; user_in_group?: boolean }
  containerlab?: { installed?: boolean; version?: string | null }
  sysctl?: Record<string, string | number | null>
  images?: string[]
  remote_root?: { path?: string; exists?: boolean }
  python3?: { installed?: boolean; version?: string | null }
  rsync?: boolean
  sudo_nopasswd?: boolean
  pipx?: boolean
  recommendations?: string[]
  raw?: Record<string, { rc: number; stdout: string; stderr: string }>
  [key: string]: unknown
}

export interface HostCapabilitiesOut {
  host_id: string
  last_probe_at: string | null
  capabilities: HostCapabilities
}

export interface HostSummary {
  os?: string | null
  docker?: string | null
  containerlab?: string | null
  recommendations?: number
  arch?: string | null
  platform?: 'linux' | 'wsl' | 'orbstack' | string | null
  platform_label?: string | null
  beta?: boolean
}

export interface Host {
  id: string
  name: string
  kind: HostKind
  address: string
  ssh_port: number
  ssh_user: string
  ssh_key_secret_id: string | null
  password_secret_id: string | null
  sudo_password_secret_id?: string | null
  /** 1.0.1: '[user@]host[:port]' SSH jump host (same credentials) — the Mac/PC in front of its Linux VM */
  jump_host?: string | null
  /** 1.0.2: what the user says the host is; mac/windows = a Linux VM behind that machine (beta) */
  platform?: 'linux' | 'mac' | 'windows' | null
  last_probe_at: string | null
  created_at: string
  summary: HostSummary
}

export interface HostIn {
  name: string
  kind: HostKind
  address: string
  ssh_port: number
  ssh_user: string
  ssh_key_secret_id?: string | null
  password_secret_id?: string | null
  sudo_password_secret_id?: string | null
  /** 1.0.1: '[user@]host[:port]' SSH jump host (same credentials) — the Mac/PC in front of its Linux VM */
  jump_host?: string | null
  /** 1.0.2: what the user says the host is; mac/windows = a Linux VM behind that machine (beta) */
  platform?: 'linux' | 'mac' | 'windows' | null
}

export interface GeneratedKey {
  secret_id: string
  name: string
  public_key: string
}

export type BootstrapRecipe = 'clab-host' | 'act-tools'
export interface BootstrapIn {
  recipe: BootstrapRecipe
  options: { native_toolchain: boolean; clab_mode: 'native' | 'container' }
}

export interface DevcontainerIn {
  mode: 'local-clab' | 'remote-host'
  avd_version?: string
  include_vscode_extensions: boolean
}

// ---- images
export interface Image {
  id: string
  fmt: string
  version: string
  docker_tag: string
  local_path: string | null
  sha256: string | null
  size_bytes: number | null
  present_on_hosts: string[]
  /** CONTRACT-3 §6: derived from fmt (cEOSarm → arm64) */
  arch?: Arch | null
  created_at: string
}

export type Arch = 'x86_64' | 'arm64'

/** GET /images/formats */
export interface ImageFormat {
  fmt: string
  arch: Arch
  label: string
}

export interface ImageToken {
  configured: boolean
  secret_id: string | null
  name: string | null
  created_at: string | null
  hint: string | null
}

export interface ImageCatalog {
  fmt: string
  versions: string[]
}

// ---- templates / labs
export type KnobType = 'int' | 'string' | 'cidr' | 'ipv4' | 'pool' | 'secret' | 'object' | 'yaml' | 'bool'

export interface Knob {
  key: string
  type: KnobType
  default?: unknown
  label?: string | null
  help?: string | null
  min?: number | null
  max?: number | null
  choices?: unknown[] | null
  path?: string | null
  /** CONTRACT-6 §3: knob help (summary, avd_keys, renders, pitfalls, doc_url) */
  learn?: import('./phase6').KnobLearn | null
}

export interface Template {
  name: string
  title: string
  description: string
  fabric_name?: string
  generator?: 'l3ls' | 'static' | 'none'
  knobs: Knob[]
  /** CONTRACT-6 §2 catalogue metadata */
  tags?: string[]
  node_count?: number | null
  learn?: string | null
  notes?: string | null
}

export interface Lab {
  id: string
  name: string
  slug: string
  template: string
  host_id: string | null
  host?: Host | null
  target: 'clab' | 'act'
  state: LabState
  ceos_version: string | null
  knobs?: Record<string, unknown>
  /** knob keys whose files were edited by hand (contract: "marks knobs as custom") */
  custom_knobs?: string[]
  settings?: Record<string, unknown>
  last_build_job?: Job | null
  /** bound CloudVision target (CONTRACT-2c) */
  cv_target_id?: string | null
  /** bound NetBox target (CONTRACT-4 B) */
  netbox_target_id?: string | null
  created_at: string
  updated_at: string
}

export interface LabCreateIn {
  name: string
  template: string
  host_id?: string | null
  ceos_version?: string | null
  knobs: Record<string, unknown>
  /** CONTRACT-4 D: clab (default) | act */
  target?: LabTarget
}

export type LabTarget = 'clab' | 'act'

export interface LabPatchIn {
  host_id?: string | null
  ceos_version?: string | null
  knobs?: Record<string, unknown>
  cv_target_id?: string | null
  netbox_target_id?: string | null
  target?: LabTarget
}

export interface Violation {
  path?: string
  message: string
}

export interface FileEntry {
  path: string
  type?: 'file' | 'dir'
  size?: number
  editable?: boolean
  knob_managed?: boolean
  children?: FileEntry[]
}

export interface FileContent {
  path: string
  content: string
}

/** labs/service.py history() */
export interface Revision {
  sha: string
  short: string
  message: string
  author?: string
  date: string
  files?: string[]
}

export interface LabDiff {
  a: string
  b: string
  diff: string
  files?: { path: string; diff: string }[]
}

export interface Device {
  hostname: string
  type: string
  platform: string
  mgmt_ip: string | null
  serial: string | null
  loopback0: string | null
  vtep: string | null
  bgp_as: string | number | null
  mgmt_interface?: string | null
  is_deployed?: boolean
  /** CloudVision device tags {label: value}: global + per-device merged (per-device wins), topology_hint_* excluded */
  tags?: Record<string, string>
}

/** labs/build.py BuildResult.summary() */
export interface BuildResult {
  hosts?: string[]
  warnings?: string[]
  changed_files?: string[]
  fabric_name?: string
  anta_tests?: Record<string, number>
}

// ---- topology
// ---- topology (backend/avdlab/topology/models.py TopologyGraph)
export interface TopoNode {
  name: string
  kind: string
  mgmt_ip?: string | null
  role?: string | null
  serial?: string | null
  image?: string | null
}

export interface TopoLink {
  a: string
  a_if: string
  b: string
  b_if: string
  a_port?: string | null
  b_port?: string | null
  type?: string | null
}

export interface Topology {
  lab_id?: string
  name?: string
  mgmt_subnet?: string | null
  nodes: TopoNode[]
  links: TopoLink[]
  /** CONTRACT-5 §1: stored cosmetic layout, when present */
  layout?: TopologyLayout | null
}

// ---- runtime: deploy / destroy / status / validate (docs/CONTRACT-2b.md)
export interface DeployParams {
  reconfigure: boolean
  wait_eapi: boolean
  wait_timeout: number
}

export interface LabNodeStatus {
  name: string
  kind: string
  state: string
  mgmt_ip: string | null
  image: string | null
  eapi: boolean
}

export interface LabStatus {
  deployed: boolean
  nodes: LabNodeStatus[]
}

export interface ValidateParams {
  extra_fabric_validation: boolean
  skip_tests: string[]
  user_catalogs: boolean
  tags: string[]
}

export type AntaStatus = 'success' | 'failure' | 'error' | 'skipped' | 'unset'
export const ANTA_STATUSES: AntaStatus[] = ['success', 'failure', 'error', 'skipped', 'unset']

export type ValidationTotals = Record<AntaStatus, number>

export interface ValidationRun {
  id: string
  job_id: string | null
  created_at: string
  totals: ValidationTotals
  report_path: string | null
}

/** one row of anta_report.json (ANTA TestResult) */
export interface AntaResult {
  name: string
  test: string
  categories: string[]
  description: string
  result: AntaStatus
  messages: string[]
  custom_field: string | null
}

export type ReportFormat = 'json' | 'md' | 'csv'

// ---- CloudVision (CONTRACT-2c)
export type CVKind = 'cvaas' | 'onprem'

export interface CVTarget {
  id: string
  name: string
  kind: CVKind
  server: string
  verify_certs: boolean
  has_token: boolean
  has_onboarding_token: boolean
  onboarding_token_meta: { expires_at?: string | null } | null
  /** CONTRACT-4 A: a stored ZTP bootstrap script (write-only) */
  has_ztp_script?: boolean
  ztp_script_meta?: { updated_at?: string | null } | null
  created_at: string
}

/** POST/PATCH body; tokens are write-only: omit = keep, null = remove */
export interface CVTargetIn {
  name: string
  kind: CVKind
  server: string
  verify_certs: boolean
  token?: string | null
  onboarding_token?: string | null
  onboarding_token_expires_at?: string | null
  /** write-only: omit = keep, null/'' = remove */
  ztp_script?: string | null
}

export interface CVTestResult {
  ok: boolean
  devices_seen: number | null
  error: string | null
}

export interface CVOnboardNode {
  name: string
  serial: string | null
  terminattr_running: boolean
  streaming: boolean
}

export interface CVOnboarding {
  nodes: CVOnboardNode[]
  checked_at: string | null
}

export interface CVDeployDevice {
  hostname: string
  serial: string | null
  exists_on_cv: boolean
  streaming: boolean
}

export interface CVLastDeploy {
  /** null when the run failed before CloudVision created a workspace (auth, unreachable, playbook error) */
  workspace: { id?: string | null; name?: string | null; state?: string | null; url?: string | null } | null
  change_control: { id?: string | null; state?: string | null } | null
  devices?: CVDeployDevice[] | null
  errors?: string[] | null
  warnings?: string[] | null
  finished_at?: string | null
  /** 0.8.0: from the backend summary */
  ok?: boolean
  at?: number
  job_id?: string | null
}

export interface CVStatus {
  target: CVTarget | null
  onboarding: CVOnboarding | null
  last_deploy: CVLastDeploy | null
}

export interface CVDeployParams {
  submit_workspace: boolean
  run_change_control: boolean
  skip_missing_devices: boolean
  strict_tags: boolean
  workspace_name?: string
}

// ---- CONTRACT-3 §1: clients & traffic
export interface ClientLink { eth: string; switch: string; switch_port: string }
export interface ClientVlan {
  vid: number
  iface: string
  ip: string | null
  gateway: string | null
  default_route: boolean
  tenant?: string | null
  vrf?: string | null
}
export interface ClientPlan {
  name: string
  image: string
  links: ClientLink[]
  bond: { name: string; members: string[] } | null
  vlans: ClientVlan[]
}
export interface LabClient extends ClientPlan {
  container_state?: string | null
  mgmt_ip?: string | null
  /** CONTRACT-6 §4: plugged in without a fabric redeploy */
  hot_added?: boolean
  deployed?: boolean
}

export interface PingParams {
  pairs: { src: string; dst: string }[] | null
  count: number
  size: number
  vlan?: number
}
export interface PingResultRow {
  src: string
  dst: string
  target_ip: string | null
  sent: number
  received: number
  loss_pct: number
  rtt_avg_ms: number | null
  ok: boolean
}
export interface PingResult {
  results: PingResultRow[]
  matrix: Record<string, Record<string, number | null>>
}

export type IperfProto = 'tcp' | 'udp'
export interface IperfFlow {
  src: string
  dst: string
  proto: IperfProto
  bandwidth: string | null
  duration: number
  parallel: number
  reverse: boolean
}
export interface IperfParams { flows: IperfFlow[]; sequential: boolean }
export interface IperfResultRow {
  src: string
  dst: string
  proto: IperfProto
  seconds: number
  bits_per_second_sent: number
  bits_per_second_received: number
  retransmits: number | null
  jitter_ms: number | null
  lost_percent: number | null
  ok: boolean
  error: string | null
}

export type TrafficKind = 'ping' | 'iperf' | 'mss'
export interface TrafficRun {
  id: string
  lab_id?: string
  job_id: string | null
  kind: TrafficKind
  params: Record<string, unknown>
  /** ping → PingResult; iperf → {results: IperfResultRow[]} or a bare list (normalised in lib/traffic) */
  results?: unknown
  created_at: string
}

// ---- CONTRACT-3 §3: running configs & snapshots
export interface RunningConfig { node: string; config: string; fetched_at: string }
export interface ConfigDiff { node: string; intended: string; running: string; diff: string; identical: boolean }
export interface Snapshot { id: string; created_at: string; nodes: number; commands: string[] }

// ---- CONTRACT-3 §2: git coupling
export type GitAuthKind = 'none' | 'token' | 'ssh_key'
export interface GitSync { at: string; direction: 'push' | 'pull' | string; ok: boolean; message: string }
export interface GitStatus {
  configured: boolean
  url?: string | null
  branch?: string | null
  auth_kind?: GitAuthKind | null
  has_secret?: boolean
  ahead?: number | null
  behind?: number | null
  last_sync?: GitSync | null
}
export interface GitConfigIn {
  url: string
  branch: string
  auth_kind: GitAuthKind
  secret_id?: string | null
  /** write-only: omitted = keep the stored one */
  token?: string | null
}
export interface GitConfigOut { url: string; branch: string; auth_kind: GitAuthKind; has_secret: boolean }
export interface GitImportIn extends GitConfigIn { name: string; host_id?: string | null }

// ---- CONTRACT-3 §8: VS Code
export interface OpenLinks {
  remote_ssh: string
  clab_topology_path: string
  remote_ssh_topology: string
  devcontainer: string | null
  ssh_command: string
  hints: string[]
}

// ---- CONTRACT-3 §5: host monitoring
export interface DiskUsage { size: number; used: number }
export interface HostMetricPoint {
  at: string
  cpu_pct: number | null
  load1: number | null
  load5: number | null
  load15: number | null
  mem_total: number | null
  mem_used: number | null
  mem_available: number | null
  disk: Record<string, DiskUsage>
}
export interface ContainerMetricPoint { at: string; cpu_pct: number | null; mem_used: number | null; mem_limit: number | null }
export interface HostMetrics {
  host_id: string
  points: HostMetricPoint[]
  containers: Record<string, ContainerMetricPoint[]>
  sampled_at: string | null
  watched: boolean
}
export interface HostProcess { pid: number; user: string; cpu_pct: number; mem_pct: number; rss: number; command: string }
export interface HostProcesses { at: string; processes: HostProcess[] }
export interface HostContainer { name: string; image: string; status: string; state: string; labels: Record<string, string> }
/** DELETE /hosts/{id}/containers/{name} (CONTRACT-3b §3) */
export interface ContainerKillResult { name: string; action: 'stop' | 'rm'; removed: boolean; returncode: number; output: string }

// ---- CONTRACT-4 A: DHCP + ZTP provisioning
export type ProvisioningMode = 'startup-config' | 'ztp'
export type ZtpSource = 'intended' | 'cloudvision'
export interface ProvisioningKnob { mode: ProvisioningMode; ztp_source: ZtpSource; ztp_server_ip: string | null }
export interface ZtpNode {
  name: string
  mac: string | null
  ip: string | null
  lease: boolean
  bootfile_fetched: boolean
  eapi: boolean
  bootfile: string | null
}
/** GET /labs/{id}/ztp */
export interface ZtpStatus {
  mode: ProvisioningMode
  source?: ZtpSource | null
  server_ip: string | null
  nodes: ZtpNode[]
  at?: number | string | null
  fresh?: boolean
  error?: string | null
}

// ---- CONTRACT-4 B: NetBox
export interface NetBoxTarget {
  id: string
  name: string
  url: string
  verify_certs: boolean
  has_token: boolean
  token_meta: { updated_at?: string | null } | null
  created_at: string | null
  labs: { id: string; name: string }[]
}
/** POST/PATCH body; token write-only: omit = keep, null/'' = remove */
export interface NetBoxTargetIn {
  name: string
  url: string
  verify_certs: boolean
  token?: string | null
}
export interface NetBoxTestResult { ok: boolean; version: string | null; error: string | null }
export interface NetBoxSyncParams { delete_stale: boolean; dry_run: boolean; include_clients: boolean }
export interface NetBoxSyncResult {
  created: Record<string, number>
  updated: Record<string, number>
  deleted: Record<string, number>
  unchanged?: Record<string, number>
  skipped: string[]
  netbox_url: string | null
  dry_run?: boolean
  at?: string | null
  target?: { id: string; name: string } | null
  job_id?: string | null
}
export interface NetBoxStatus { target: NetBoxTarget | null; last_sync: NetBoxSyncResult | null }

// ---- CONTRACT-4 C: MSS foundation (knob `segmentation`)
export type MssProtocol = 'tcp' | 'udp' | 'icmp' | 'any'
export type MssAction = 'forward' | 'drop'
export interface MssApplication { name: string; protocol: MssProtocol; ports: string | null }
export interface MssRule { application: string; action: MssAction; log: boolean }
export interface MssPolicy { name: string; rules: MssRule[] }
export interface MssSegmentPolicy { from: string; policy: string }
export interface MssSegment {
  name: string
  prefixes: string[]
  interfaces: string[]
  policies: MssSegmentPolicy[]
  fallback: MssAction | null
}
export interface MssVrf { name: string; segments: MssSegment[] }
export interface Segmentation {
  enabled: boolean
  applications: MssApplication[]
  policies: MssPolicy[]
  vrfs: MssVrf[]
}
export interface MssLastVerify { run_id: string; job_id: string | null; passed: number | null; failed: number | null; created_at: string }
/** GET /labs/{id}/mss */
export interface MssSummary extends Segmentation {
  matrix: Record<string, Record<string, Record<string, string | null>>>
  rendered_on: string[]
  last_verify: MssLastVerify | null
}
export type MssExpect = 'allow' | 'deny'
export interface MssCase { src: string; dst: string; application: string; expect: MssExpect; vlan?: number | null }
export interface MssVerifyParams { cases: MssCase[] | null }
export interface MssCaseResult extends MssCase {
  observed: 'allow' | 'deny' | 'error'
  pass: boolean
  detail?: string | null
  target_ip?: string | null
}
export interface MssVerifyResult { cases: MssCaseResult[]; passed: number; failed: number }

// ---- CONTRACT-4 D: Arista Cloud Test
export interface ActKnob { os_version: string; username: string; password: string; internet_access: boolean; tools_server_ip: string }
export type ActRole = 'switch' | 'tools-server' | 'client'
export interface ActNode { name: string; node_type: string; ip_addr: string | null; ip: string | null; version: string | null; role: ActRole }
export interface ActClient { name: string; ip_addr: string | null; links: string[][]; script_path: string | null; script: string | null }
/** GET /labs/{id}/act */
export interface ActInfo {
  target: LabTarget
  prepared: boolean
  topology_path: string
  tools_server_ip: string | null
  nodes: ActNode[]
  clients: ActClient[]
  apply_script_path: string | null
  hints: string[]
}

// ---- CONTRACT-5 §1: editable topology
export interface TopologyLayout {
  positions: Record<string, { x: number; y: number }>
  locked: boolean
}

/** GET /topology/{id}/ports */
export type TopologyPorts = Record<string, { used: string[]; free: string[] }>

export interface EndpointAdapter {
  switches: string[]
  switch_ports: string[]
  endpoint_ports: string[]
  mode: 'access' | 'trunk'
  vlans: string
  native_vlan?: number
  port_channel?: { mode: 'active' | 'passive' | 'on' }
}

export type EditOp =
  | { op: 'set_uplink'; node: string; index: number; switch: string; switch_port: string }
  | { op: 'add_endpoint'; name: string; type: string; adapters: EndpointAdapter[] }
  | { op: 'remove_endpoint'; name: string }
  | { op: 'update_endpoint'; name: string; adapters: EndpointAdapter[] }
  | { op: 'add_leaf_pair' }
  | { op: 'remove_last_leaf_pair' }
  | { op: 'set_l2leaf_per_pair'; n: number }
  | { op: 'set_spine_count'; n: number }

export interface EditRequest { ops: EditOp[]; dry_run: boolean }
export interface EditResult { graph: Topology; changed_files: string[]; warnings: string[] }
/** normalised 422 body of /edit (raw violations may be strings or {path, message}) */
export interface EditRejection { message: string; violations: Violation[]; op_index: number | null }

// ---- CONTRACT-5 §2/§4: meta, changelog, notices
export type ChangelogSections = Record<string, string[]>
export interface ChangelogEntry { version: string; date: string | null; sections: ChangelogSections }
export interface Meta {
  version: string
  git_sha: string | null
  build_date: string | null
  avd_version: string | null
  anta_version: string | null
  python: string | null
  changelog_latest: ChangelogEntry | null
  // v0.9.0 (CONTRACT-8 A): public app settings — optional so older backends/mocks still type-check
  app_title?: string
  github_url?: string | null
  support_email?: string | null
  /** 0.10.0: recipient for the mailto feedback button */
  feedback_email?: string | null
  registration_enabled?: boolean
  intro_video_url?: string | null
  copyright?: string
  copyright_notice?: string
}
export interface Changelog { entries: ChangelogEntry[]; markdown: string }
export interface Notices { markdown: string; copyright: string; license: string }

// ---- CONTRACT-5 §3: feedback
export type FeedbackKind = 'bug' | 'suggestion' | 'question'
export type FeedbackStatus = 'new' | 'triaged' | 'done'
export interface FeedbackIn { kind: FeedbackKind; title: string; body: string; page: string; include_diagnostics: boolean }
export interface FeedbackCreated { id: string | number; github_issue_url: string | null; diagnostics: Record<string, unknown> | null }
export interface Feedback {
  id: string | number
  kind: FeedbackKind
  title: string
  body: string
  page: string
  status: FeedbackStatus
  github_issue_url: string | null
  created_at: string
}

// ---- v0.8.0 (CONTRACT-7 stream A): users, roles, MFA
export type UserRole = 'admin' | 'operator' | 'viewer'
/** GET /auth/me and a completed login (`user` stays the plain username) */
export interface AuthMe extends Me {
  role: UserRole
  must_change_password: boolean
  mfa_enabled: boolean
  recovery_codes_left?: number
}
export type LoginResult = AuthMe | { mfa_required: true }
export interface PasswordPolicy { min_length: number; classes_required: number; max_repeat: number }
export interface MfaSetup { secret: string; otpauth_url: string; qr_svg: string }
export interface RecoveryCodes { recovery_codes: string[] }
export interface UserAccount {
  id: string
  username: string
  role: UserRole
  disabled: boolean
  must_change_password: boolean
  mfa_enabled: boolean
  recovery_codes_left: number
  failed_logins: number
  locked: boolean
  locked_until: string | null
  last_login_at: string | null
  password_changed_at: string | null
  created_at: string | null
}
/** create / reset-password responses carry the temporary password exactly once */
export interface UserWithTemp extends UserAccount { temporary_password: string | null }
export interface UserCreateIn { username: string; password?: string; role: UserRole; send_temp?: boolean }
export interface UserPatchIn { role?: UserRole; disabled?: boolean }
export interface AuditEntry {
  id: number
  at: string | null
  username: string
  event: string
  ip: string | null
  user_agent: string | null
  detail: string | null
}

// ---- v0.9.0 (CONTRACT-8 A): account requests + admin settings
export type AccountRequestStatus = 'pending' | 'approved' | 'rejected'
export interface RegisterIn { username: string; email: string; full_name: string; reason: string }
export interface RegisterResult { id: string; status: AccountRequestStatus }
export interface AccountRequest {
  id: string
  username: string
  email: string
  full_name: string
  reason: string
  status: AccountRequestStatus
  ip: string | null
  role: UserRole | null
  user_id: string | null
  decided_by: string | null
  decided_at: string | null
  decision_reason: string | null
  created_at: string | null
}
export interface ApproveResult { user: UserAccount; temporary_password: string; request: AccountRequest; mail_queued: boolean }
export interface AppSettingsValues {
  app_title: string
  github_url: string
  support_email: string
  registration_enabled: boolean
  intro_video_url: string
  feedback_mail_to: string
  feedback_mail_enabled: boolean
  registration_mail_to: string
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_starttls: boolean
  smtp_from: string
}
export interface AppSettings extends AppSettingsValues {
  has_smtp_password: boolean
  smtp_configured: boolean
  overridden: string[]
  defaults: Partial<AppSettingsValues>
}
/** PUT /admin/settings: partial; `null` resets a key to its env default; `smtp_password: ""` clears the password */
export type AppSettingsIn = { [K in keyof AppSettingsValues]?: AppSettingsValues[K] | null } & { smtp_password?: string | null }
export interface TestMailResult { ok: boolean; error: string | null; to: string }

// ---- v0.10.0: AI lab designer (bring your own model)
export type AiProvider = 'anthropic' | 'openai'
export interface AiLinkInfo { provider: AiProvider; base_url: string; model: string; has_api_key: boolean; created_at: string | null; updated_at: string | null }
/** GET /ai/link — the user's own link (never the key) + whether the admin's shared model is available */
export interface AiLinkView {
  linked: boolean
  link: AiLinkInfo | null
  shared: { available: boolean; provider: AiProvider | null; model: string | null }
  effective: 'user' | 'shared' | null
}
/** PUT /ai/link — api_key write-only: absent/null keeps the stored key, "" removes it */
export interface AiLinkIn { provider: AiProvider; base_url: string; model: string; api_key?: string | null }
export interface AiProbeResult { ok: boolean; models: string[]; model_listed: boolean | null; error: string | null }
export interface AiChatTurn { role: 'user' | 'assistant'; content: string }
export interface AiDesignIn { lab_id?: string; template?: string; prompt: string; history?: AiChatTurn[]; current_knobs?: Record<string, unknown> }
export interface AiTopologyOp { op: string; [k: string]: unknown }
export interface AiKnobChange { key: string; old: unknown; new: unknown }
export interface AiDryRun {
  valid: boolean
  violations: Violation[]
  changed_files: string[]
  diff: string
  warnings?: string[]
  graph?: Topology | null
  ops?: string[]
  nodes?: number
}
export interface AiDesignOut {
  kind: 'proposal' | 'question' | 'text'
  summary: string
  assumptions: string[]
  knobs: Record<string, unknown>
  knob_changes: AiKnobChange[]
  topology_ops: AiTopologyOp[]
  dry_run: AiDryRun | null
  question?: string
  text?: string
  assistant_message: string
  repaired: boolean
  source: 'user' | 'shared'
  provider: AiProvider
  model: string
  /** 0.10.0 from scratch (no lab_id/template in the request): the template the model picked */
  template?: string
  template_reason?: string
  lab_name?: string
}
export interface AiApplyIn { lab_id: string; knobs: Record<string, unknown>; topology_ops: AiTopologyOp[] }
export interface AiApplyOut { lab: Lab; knob_changes: AiKnobChange[]; topology: { commit: string | null; changed_files: string[]; warnings: string[] } | null; notes: string[] }
/** GET/PUT /admin/ai — shared default model (admin) */
export interface AiSharedSettings {
  ai_default_provider: AiProvider | ''
  ai_default_base_url: string
  ai_default_model: string
  ai_shared_allowed: boolean
  has_ai_default_api_key: boolean
  configured: boolean
}
export type AiSharedSettingsIn = Partial<Pick<AiSharedSettings, 'ai_default_provider' | 'ai_default_base_url' | 'ai_default_model' | 'ai_shared_allowed'>> & { ai_default_api_key?: string | null }

/** GET /labs/{id}/push/plan (0.10.0) */
export interface PushPlan {
  available: boolean
  changed: string[]
  unchanged: string[]
  unknown: string[]
  total: number
  since: number | null
  last_job_id: string | null
}

/** GET /labs/{id}/psirt (0.10.0, ANTA security advisories preview) */
export type PsirtResult = 'affected' | 'inconclusive' | 'mitigated' | 'not affected' | 'error' | 'skipped'
export interface PsirtAssessment { device: string; advisory_id: string; test: string; title: string; severity: string; url: string; result: PsirtResult | string }
export interface PsirtRow {
  device: string; test: string; advisory_id: string; advisory_title: string; advisory_severity: string; advisory_url: string
  advisory_result: string; advisory_messages: string; vulnerability_id: string; vulnerability_severity: string
  vulnerability_result: string; vulnerability_messages: string; remediation: string
}
export interface PsirtLast {
  at: number; job_id: string; tests: string[]; devices: string[]; duration: number
  summary: Record<PsirtResult, number>; by_device: Record<string, Record<PsirtResult, number>>
  advisories: { id: string; title: string; severity: string; url: string }[]
  assessments: PsirtAssessment[]; rows: PsirtRow[]; has_md: boolean
}
export interface PsirtStatus { available: boolean; last: PsirtLast | null }

