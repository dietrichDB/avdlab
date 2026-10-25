import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { hostsApi } from '../api/hosts'
import { imagesApi } from '../api/images'
import { labsApi } from '../api/labs'
import { Icon, LogoMark, type IconName } from '../components/Brand'
import { useTour } from '../components/learn/Tour'
import { useMeta } from '../hooks/useMeta'
import { useUiMode } from '../lib/uiMode'
import { PageHeader } from '../components/ui'
import { Term } from '../components/learn/Term'
import { cls } from '../lib/format'

const DONE_KEY = 'avdlab.gettingStarted.done'

/* ------------------------------------------------------------------------------------------------ content */
interface Step {
  id: string
  n: number
  title: string
  icon: IconName
  short: string
  what: ReactNode
  why: ReactNode
  how: string[]
  to: string
  cta: string
  tour?: string
  /** how the page decides the step is done */
  done?: (s: Facts) => boolean
  beta?: string
}
interface Facts { hosts: number; images: number; labs: number; built: number; deployed: number; validated: number; cv: number }

const STEPS: Step[] = [
  {
    id: 'host', n: 1, title: 'Add a lab host', icon: 'server', short: 'a Linux box that runs the switches',
    what: <>A <b>lab host</b> is a Linux machine (VM or bare metal) where the virtual switches run as containers. avdlab connects to it over SSH, installs Docker and <Term k="containerlab">containerlab</Term>, and keeps an eye on CPU and memory.</>,
    why: <>cEOS switches are containers; something has to run them. 8 switches need roughly 16 GB of RAM.</>,
    how: ['Hosts → Add host: name, IP, SSH user and key (or password)', 'Probe shows what is installed; Bootstrap installs what is missing', 'A green "ready" badge means the host can run a lab'],
    beta: 'No Linux box? A Linux VM on your Mac (OrbStack) or Windows PC (the Containerlab WSL2 distribution) works too — supported as a beta feature, see the README.',
    to: '/hosts', cta: 'Open Hosts', tour: 'hosts', done: (f) => f.hosts > 0,
  },
  {
    id: 'image', n: 2, title: 'Download a cEOS image', icon: 'image', short: 'the switch software, once',
    what: <>A <b>cEOS-lab image</b> is Arista EOS packaged as a container. Images come from the Arista software download portal with your arista.com API token, are stored once in avdlab and pushed to the hosts that need them.</>,
    why: <>Every switch in every lab boots from this image; pick one EOS version and reuse it everywhere.</>,
    how: ['Images → save your arista.com token (My Profile → Access Token)', 'Pick a version in the catalogue (4.34.x is a safe choice) → Download', 'Push it to your host — or let the first deploy do it'],
    to: '/images', cta: 'Open Images', done: (f) => f.images > 0,
  },
  {
    id: 'lab', n: 3, title: 'Create a lab', icon: 'labs', short: 'pick a design, turn the knobs',
    what: <>A <b>lab</b> is one AVD fabric: a template (an official Arista <Term k="AVD">AVD</Term> example such as a single-DC spine/leaf) plus your <b>knobs</b> — how many spines and leaf pairs, IP pools, VLANs and tenants, CloudVision mode. Or describe it in a sentence and let the AI designer fill the knobs.</>,
    why: <>The knobs <em>are</em> the AVD inputs. Everything else (configs, topology, tests) is generated from them, so there is nothing else to keep in sync.</>,
    how: ['Labs → New lab → choose a template (or "Start from a sentence")', 'Every knob has an (i) with what it writes into the AVD inputs', 'Name the lab, pick the host and the image → Create'],
    to: '/labs/new', cta: 'New lab', tour: 'first-lab', done: (f) => f.labs > 0,
  },
  {
    id: 'build', n: 4, title: 'Build', icon: 'code', short: 'inputs → configs, in seconds',
    what: <><b>Build</b> runs AVD in-process: your inputs become <Term k="structured config">structured configs</Term>, EOS CLI configs, documentation and an <Term k="ANTA">ANTA</Term> test catalogue — the same output Arista's own examples ship with.</>,
    why: <>You can read and diff every generated line before anything touches a switch; the explainer tells you which knob produced which line.</>,
    how: ['Lab → Build tab → Build', 'Browse the generated files on the Files tab; hover a line for its origin', 'Topology tab shows the fabric as AVD sees it — you can edit it there'],
    to: '/labs', cta: 'Open Labs', done: (f) => f.built > 0,
  },
  {
    id: 'deploy', n: 5, title: 'Deploy', icon: 'play', short: 'bring the fabric up on the host',
    what: <><b>Deploy</b> writes a containerlab topology from the lab, starts the cEOS containers on the host — each boots from its generated config as startup-config — and waits for their API. Later config changes go out with <b>Push config changes</b> over eAPI, without a restart.</>,
    why: <>In a few minutes you have a running EVPN/VXLAN fabric you can log into (web terminal), ping through, and break.</>,
    how: ['Lab → Deploy (the image is pushed first if the host lacks it)', 'Runtime tab: node status, a terminal per switch, redeploy / destroy', 'Clients tab: add test hosts and run ping or iperf between them'],
    to: '/labs', cta: 'Open Labs', done: (f) => f.deployed > 0,
  },
  {
    id: 'validate', n: 6, title: 'Validate', icon: 'check', short: 'ANTA proves the design works',
    what: <><b>Validate</b> runs <Term k="ANTA">ANTA</Term> with the test catalogue AVD generated for this exact fabric: BGP sessions, MLAG, VXLAN, interfaces, routing — hundreds of checks in under a minute.</>,
    why: <>Green means the design and the running state agree. Red tells you precisely which device and which test to look at.</>,
    how: ['Lab → Validate', 'Validation tab: results per device and per test category, with history', 'Change a knob, build, deploy and validate again to see the difference'],
    to: '/labs', cta: 'Open Labs', tour: 'validation', done: (f) => f.validated > 0,
  },
  {
    id: 'cv', n: 7, title: 'CloudVision (optional)', icon: 'cloud', short: 'stream the lab into CVaaS or on-prem',
    what: <>Connect the lab to <b>CloudVision</b>: avdlab onboards every switch with your onboarding token (TerminAttr), then <code>cv_deploy</code> pushes the configs and tags into a CloudVision workspace.</>,
    why: <>The lab becomes a full demo of the Arista stack — telemetry, change control, Studios — on virtual switches.</>,
    how: ['CloudVision page → add a target (CVaaS region or on-prem server, service-account token, onboarding token)', 'Lab Settings → cloudvision knob mode cvaas/onprem → rebuild and redeploy', 'Lab → CloudVision tab → Onboard, then Deploy to CloudVision'],
    to: '/cloudvision', cta: 'Open CloudVision', tour: 'cloudvision', done: (f) => f.cv > 0,
  },
]

interface MenuItem { to: string; label: string; icon: IconName; text: string; advanced?: boolean; admin?: boolean }
const MENU: { group: string; items: MenuItem[] }[] = [
  { group: 'Overview', items: [{ to: '/', label: 'Dashboard', icon: 'home', text: 'Where you land: next steps, counters, the intro video and quick links.' }] },
  { group: 'Provisioning', items: [
    { to: '/hosts', label: 'Hosts', icon: 'server', text: 'The Linux machines that run the switches. Probe, bootstrap, monitor, open a terminal.' },
    { to: '/images', label: 'Images', icon: 'image', text: 'cEOS images: Arista catalogue (with your token), local store, which host has what.' },
    { to: '/labs', label: 'Labs', icon: 'labs', text: 'Your fabrics. Each lab has tabs: Overview, Settings (knobs), Files, Build, Topology, Runtime, Devices, Clients, Validation, CloudVision, Jobs.' },
    { to: '/cloudvision', label: 'CloudVision', icon: 'cloud', text: 'CloudVision targets (CVaaS or on-prem) with their tokens; the lab-side actions live on the lab\'s CloudVision tab.' },
    { to: '/netbox', label: 'NetBox', icon: 'database', text: 'Push the lab\'s devices, interfaces and IPs into a NetBox instance as a source-of-truth demo.', advanced: true },
    { to: '/devcontainer', label: 'Devcontainer', icon: 'code', text: 'Open the lab in VS Code with the AVD devcontainer and the containerlab extension.', advanced: true },
  ] },
  { group: 'Learn', items: [{ to: '/learn', label: 'Learn AVD', icon: 'book', text: 'Lessons with checks against your lab, the glossary behind every hover term, the AVD schema browser and guided tours.' }] },
  { group: 'Admin', items: [
    { to: '/users', label: 'Users', icon: 'users', text: 'Accounts, roles (admin / operator / viewer), MFA resets, account requests, audit log.', admin: true },
    { to: '/admin/settings', label: 'Admin settings', icon: 'cog', text: 'App title, GitHub link, registration, e-mail (SMTP), shared AI model.', admin: true },
    { to: '/jobs', label: 'Jobs', icon: 'jobs', text: 'Every build, deploy, validate and sync as a job with a live log; cancel from here.', advanced: true },
    { to: '/feedback', label: 'Feedback', icon: 'feedback', text: 'What people reported with the Feedback button, with diagnostics.' },
    { to: '/changelog', label: 'Changelog', icon: 'changelog', text: 'What changed in each version.' },
    { to: '/about', label: 'About', icon: 'info', text: 'Version, build, licence, third-party notices, author.' },
  ] },
]

/* ------------------------------------------------------------------------------------------------ page */
export default function GettingStartedPage() {
  const meta = useMeta()
  const tour = useTour()
  const { beginner, setMode } = useUiMode()
  const hosts = useQuery({ queryKey: ['hosts'], queryFn: hostsApi.list })
  const images = useQuery({ queryKey: ['images'], queryFn: imagesApi.list })
  const labs = useQuery({ queryKey: ['labs'], queryFn: labsApi.list })
  const facts: Facts = {
    hosts: hosts.data?.length ?? 0, images: images.data?.length ?? 0, labs: labs.data?.length ?? 0,
    built: (labs.data ?? []).filter((l) => ['built', 'deployed', 'validated'].includes(l.state)).length,
    deployed: (labs.data ?? []).filter((l) => ['deployed', 'validated'].includes(l.state)).length,
    validated: (labs.data ?? []).filter((l) => l.state === 'validated').length,
    cv: (labs.data ?? []).filter((l) => !!l.cv_target_id).length,
  }
  const [active, setActive] = useState<string>(STEPS[0].id)
  const [done, setDone] = useState<boolean>(() => { try { return localStorage.getItem(DONE_KEY) === '1' } catch { return false } })
  useEffect(() => { try { localStorage.setItem(DONE_KEY, done ? '1' : '0') } catch { /* private mode */ } }, [done])
  const step = STEPS.find((s) => s.id === active) ?? STEPS[0]
  const completed = STEPS.filter((s) => s.done?.(facts)).length

  return (
    <div className="space-y-6" data-testid="getting-started">
      <PageHeader title="Getting started" sub={<>How {meta.data?.app_title ?? 'avdlab'} turns a few knobs into a running, validated Arista fabric — and what every menu item is for.</>}
        actions={<label className="inline-flex items-center gap-2 text-sm"><input type="checkbox" checked={done} onChange={(e) => setDone(e.target.checked)} data-testid="gs-done" /> I've read this</label>} />

      {/* ---- the journey: 7 stations, hover/click for details */}
      <section className="card p-4">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <LogoMark className="h-8 w-8" />
          <div>
            <div className="h2">The journey of a lab</div>
            <div className="text-xs text-cv-muted">hover or click a station — {completed}/{STEPS.length} already done on this server</div>
          </div>
          <div className="ml-auto flex items-center gap-2 text-xs text-cv-muted">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500" /> done
            <span className="ml-2 inline-block h-2.5 w-2.5 rounded-full bg-cv-primary" /> next
          </div>
        </div>
        <ol className="relative grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7" data-testid="gs-journey">
          {STEPS.map((s, i) => {
            const isDone = !!s.done?.(facts)
            const isNext = !isDone && STEPS.slice(0, i).every((p) => p.done?.(facts))
            return (
              <li key={s.id} className="relative">
                {i < STEPS.length - 1 && <span className="absolute left-1/2 top-7 hidden h-0.5 w-full bg-cv-border lg:block" aria-hidden />}
                <button type="button" onMouseEnter={() => setActive(s.id)} onFocus={() => setActive(s.id)} onClick={() => setActive(s.id)}
                  className={cls('relative z-10 flex w-full flex-col items-center gap-1.5 rounded-lg border p-3 text-center transition-colors',
                    active === s.id ? 'border-cv-primary bg-cv-primary-soft' : 'border-cv-border bg-cv-surface hover:border-cv-border-strong')}
                  data-testid={`gs-step-${s.id}`} aria-pressed={active === s.id}>
                  <span className={cls('flex h-11 w-11 items-center justify-center rounded-full text-white',
                    isDone ? 'bg-emerald-500' : isNext ? 'bg-cv-primary' : 'bg-cv-nav')}>
                    <Icon name={isDone ? 'check' : s.icon} className="h-5 w-5" />
                  </span>
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-cv-muted">step {s.n}</span>
                  <span className="text-sm font-semibold leading-tight">{s.title}</span>
                  <span className="text-[11px] text-cv-muted">{s.short}</span>
                </button>
              </li>
            )
          })}
        </ol>

        {/* ---- detail card for the active station */}
        <div className="mt-4 grid gap-4 rounded-lg border border-cv-border bg-cv-surface-2 p-4 lg:grid-cols-[1.2fr_1fr]" data-testid="gs-detail">
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-cv-nav text-white"><Icon name={step.icon} className="h-4 w-4" /></span>
              <div><div className="text-xs uppercase tracking-wide text-cv-muted">step {step.n}</div><div className="text-lg font-semibold">{step.title}</div></div>
              {step.done?.(facts) && <span className="badge ml-auto bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">done</span>}
            </div>
            <p className="text-sm"><span className="font-semibold text-cv-muted">What it is. </span>{step.what}</p>
            <p className="text-sm"><span className="font-semibold text-cv-muted">Why it matters. </span>{step.why}</p>
            {step.beta && <p className="rounded border border-violet-200 bg-violet-50 px-2 py-1 text-xs text-violet-900 dark:border-violet-900 dark:bg-violet-950/40 dark:text-violet-100"><span className="badge mr-1 bg-violet-100 text-violet-800 dark:bg-violet-900/50 dark:text-violet-200">beta</span>{step.beta}</p>}
            <div className="flex flex-wrap gap-2 pt-1">
              <Link className="btn-primary btn-sm" to={step.to}>{step.cta}</Link>
              {step.tour && tour.tours?.[step.tour] && <button type="button" className="btn-secondary btn-sm" onClick={() => tour.start(step.tour!)} data-testid="gs-tour">Guided tour</button>}
            </div>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-cv-muted">How, in three moves</div>
            <ol className="space-y-1.5 text-sm">
              {step.how.map((h, i) => (
                <li key={i} className="flex gap-2"><span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-cv-primary-soft text-[11px] font-semibold text-cv-primary-text">{i + 1}</span><span>{h}</span></li>
              ))}
            </ol>
          </div>
        </div>
      </section>

      {/* ---- what is what in the menu */}
      <section className="card p-4">
        <div className="mb-1 h2">What the menu items mean</div>
        <p className="mb-3 text-xs text-cv-muted">
          Items marked <span className="badge bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">advanced</span> only show in Advanced mode
          {beginner ? <> — you are in Beginner mode (<button type="button" className="link" onClick={() => setMode('advanced')}>switch</button>)</> : <> — you are in Advanced mode (<button type="button" className="link" onClick={() => setMode('beginner')}>switch back</button>)</>}.
          Hover a card for the one-line explanation; the same text appears as hints on the pages themselves in Beginner mode.
        </p>
        <div className="space-y-3" data-testid="gs-menu">
          {MENU.map((g) => (
            <div key={g.group}>
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-cv-muted">{g.group}</div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {g.items.map((m) => (
                  <Link key={m.to} to={m.to} className="group flex gap-3 rounded-lg border border-cv-border bg-cv-surface p-3 hover:border-cv-primary" title={m.text} data-testid="gs-menu-item">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-cv-nav text-white"><Icon name={m.icon} className="h-4 w-4" /></span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5 text-sm font-semibold">{m.label}
                        {m.advanced && <span className="badge bg-zinc-100 text-[10px] text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">advanced</span>}
                        {m.admin && <span className="badge bg-amber-100 text-[10px] text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">admin</span>}
                      </span>
                      <span className="block text-xs text-cv-muted">{m.text}</span>
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ---- vocabulary */}
      <section className="card p-4">
        <div className="mb-1 h2">Five words you will meet everywhere</div>
        <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
          {([
            ['AVD', 'Arista Validated Designs: a data model (your inputs) and the code that turns it into configs, docs and tests.'],
            ['Knob', 'One input of a lab template: spine count, VLANs, pools… Everything avdlab generates comes from knobs.'],
            ['containerlab', 'The tool that wires cEOS containers together on the lab host according to a topology file.'],
            ['ANTA', 'Arista Network Test Automation: the test framework that validates the running fabric.'],
            ['CloudVision', 'Arista\'s management platform (CVaaS in the cloud or on-prem) the lab can stream into.'],
          ] as [string, string][]).map(([t, d]) => (
            <div key={t} className="rounded-lg border border-cv-border p-3"><dt className="font-semibold"><Term k={t}>{t}</Term></dt><dd className="mt-1 text-xs text-cv-muted">{d}</dd></div>
          ))}
        </dl>
      </section>

      <div className="flex flex-wrap items-center gap-2">
        <Link className="btn-primary" to="/hosts">Start with step 1 — add a host</Link>
        <Link className="btn-secondary" to="/learn">Go deeper: Learn AVD</Link>
        <Link className="btn-secondary" to="/">Back to the dashboard</Link>
      </div>
    </div>
  )
}
