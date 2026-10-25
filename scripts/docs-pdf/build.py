"""Build the avdlab documentation PDF: docs/1.0/*.md -> styled HTML -> Chromium (Playwright) -> PDF.

    python3 scripts/docs-pdf/build.py                 # -> dist/avdlab-<version>-documentation.pdf
    python3 scripts/docs-pdf/build.py --out X.pdf --skip-shots --skip-diagrams

Stages (each cached in scripts/docs-pdf/.work/, delete it to start over):
  1. fonts     Inter + JetBrains Mono from Google Fonts (falls back to the system sans/mono when offline)
  2. diagrams  docs/1.0/diagrams/*.mmd re-rendered with the app colour theme (mermaid-theme.json) via mermaid-cli;
               very wide LR flowcharts are flipped to TB so they fit a portrait page
  3. shots     screenshots of the SPA in mock mode (shots.py starts vite on :5199 with VITE_MOCK=1)
  4. pdf       two renders: the first finds the page of every heading, the second fills the table of contents;
               the cover is rendered separately (no header/footer) and merged in front.

Needs: python3 with markdown, playwright (+ chromium), pypdf; node (npx) for mermaid-cli and vite; pdftotext (poppler).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import markdown
import argparse
import os
import shutil
import subprocess
from pypdf import PdfReader, PdfWriter
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DOCS = ROOT / "docs/1.0"
WORK = HERE / ".work"
SHOTS = WORK / "shots"
VERSION = (ROOT / "VERSION").read_text().strip()
OUT = ROOT / "dist" / f"avdlab-{VERSION}-documentation.pdf"
WORK.mkdir(exist_ok=True)
TODAY = dt.date.today().strftime("%d %B %Y")

ICONS = {}
for m in re.finditer(r"^\s+(\w+): '([^']+)',", (ROOT / "frontend/src/components/Brand.tsx").read_text(), re.M):
    ICONS[m.group(1)] = m.group(2)

LOGO = (ROOT / "frontend/public/logo.svg").read_text().replace('fill="currentColor"', 'fill="#ffffff"')
MARK = (ROOT / "frontend/public/logo-mark.svg").read_text()

CHAPTERS = [  # (file, number label, title, icon, one-line blurb)
    ("README.md", "0", "Overview", "home", "What avdlab is, how to read this book, and a five-minute quick start."),
    ("01-concepts.md", "1", "Concepts", "book", "The mental model: inputs as source of truth, the pipeline, the objects, modes and roles."),
    ("02-architecture.md", "2", "Architecture", "database", "Components, deployment topologies, the lab life cycle, job engine, data model, security and request path."),
    ("03-installation.md", "3", "Installation", "server", "LAN and Azure installs, upgrades, backups and every setting."),
    ("04-lab-hosts.md", "4", "Lab hosts", "server", "Requirements, bootstrap, Linux / Mac / Windows hosts, jump hosts, probe output, images and architecture."),
    ("05-user-guide.md", "5", "User guide", "play", "Every page and every lab tab, control by control, with screenshots."),
    ("06-ai-designer.md", "6", "AI designer", "feedback", "Design with AI and Start from a sentence: tools, dry run, repair, providers and privacy."),
    ("07-api.md", "7", "API reference", "code", "REST and WebSocket routes per router, roles, and all job types."),
    ("08-operations.md", "8", "Operations", "jobs", "Jobs and logs, troubleshooting matrix, monitoring, updating, versioning."),
    ("09-development.md", "9", "Development", "cog", "Repo layout, running locally, tests, templates and knobs, adding jobs and pages, releasing."),
]

# Screenshots inserted right after the heading whose text starts with the key (per chapter file).
SHOT_MAP = {
    "05-user-guide.md": {
        "5.2 Sign-in": [("login", "The sign-in page. Account requests go to the admin; MFA is asked for after the password.")],
        "5.3 Dashboard": [("dashboard", "Dashboard in learning mode: the getting-started card, host health bars and the lab list.")],
        "5.4 Getting started": [("getting-started", "The Getting started journey: seven stations, each one links to the page where it happens.")],
        "5.5 Hosts": [("hosts", "Hosts page with the SSH key panel and one lab host."),
                      ("add-host", "Add host with the platform picker set to Mac (OrbStack VM, beta): the steps and the jump host field appear inline."),
                      ("host-detail", "Host detail: probe, bootstrap recipe, monitor graphs and capabilities.")],
        "5.6 Images": [("images", "Images: arista.com token, catalogue and locally downloaded cEOS images.")],
        "5.7 Labs": [("labs", "The lab list with state, host, template and the last job.")],
        "5.8 New lab": [("new-lab", "New lab: pick a template, the AI card, or start from a sentence."),
                        ("new-lab-knobs", "The knobs of single-dc-l3ls after choosing the template.")],
        "Overview": [("lab-overview", "Lab overview: state, actions, the last jobs and the targets.")],
        "Files": [("lab-files", "Files: the AVD inputs and build outputs, with the editor and diff.")],
        "Build": [("lab-build", "Build: pyavd outputs, dry-run result and the incremental push card.")],
        "Topology": [("lab-topology", "Topology editor: spines, leaf pairs, test hosts and links; edits are input changes.")],
        "Runtime": [("lab-runtime", "Runtime: the containers on the lab host, console, and the push-configs card.")],
        "Devices": [("lab-devices", "Devices: per switch management IP, version, eAPI and web terminal.")],
        "Clients & test hosts": [("lab-clients", "Clients & test hosts: add Linux test hosts behind leaf ports and run traffic.")],
        "Validation": [("lab-validation", "Validation: ANTA runs, catalogs and reports."),
                       ("lab-psirt", "The ANTA PSIRT card (preview): a security-advisory scan of the running switches.")],
        "CloudVision": [("lab-cloudvision", "CloudVision tab: target, onboarding, workspace and change-control deploy.")],
        "5.10 CloudVision page": [("cloudvision", "CloudVision targets: CVaaS regions or an on-prem CVP, with a service-account token.")],
        "5.13 Learn": [("learn", "Learn AVD: guided curriculum with the lab as the exercise.")],
        "5.14 Account": [("account", "Account: password, TOTP MFA, API tokens.")],
        "5.15 Users": [("users", "Users (admin): roles, pending registrations, MFA state.")],
        "5.16 Admin settings": [("admin-settings", "Admin settings: registration, GitHub link, feedback mail, SMTP and the shared AI link.")],
    },
}

FONT_CSS_URL = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap"


def fonts_css() -> str:
    """Download Inter / JetBrains Mono once (OFL licensed) and return @font-face rules; empty string when offline."""
    d = WORK / "fonts"
    d.mkdir(exist_ok=True)
    local = d / "local.css"
    if local.exists():
        return local.read_text()
    try:
        css = subprocess.run(["curl", "-sSf", "-m", "30", "-A", "Mozilla/5.0", FONT_CSS_URL], check=True, capture_output=True, text=True).stdout
        rules = []
        for m in re.finditer(r"font-family: '([^']+)';\s*font-style: normal;\s*font-weight: (\d+);\s*font-display: swap;\s*src: url\((https://[^)]+)\)", css):
            fam, w, url = m.groups()
            fn = d / f"{fam.replace(' ', '')}-{w}.ttf"
            subprocess.run(["curl", "-sSf", "-m", "60", "-o", str(fn), url], check=True)
            rules.append(f"@font-face{{font-family:'{fam}';font-weight:{w};src:url('file://{fn}') format('truetype');}}")
        local.write_text("\n".join(rules))
        # mermaid-cli and <img src=svg> cannot use page fonts: install them for the user so the diagrams match
        user_fonts = Path.home() / ".fonts"
        user_fonts.mkdir(exist_ok=True)
        for f in d.glob("*.ttf"):
            shutil.copy(f, user_fonts / f.name)
        subprocess.run(["fc-cache", "-f"], check=False, capture_output=True)
        return local.read_text()
    except (subprocess.CalledProcessError, OSError) as e:
        print("  ! fonts not downloaded, using system fonts:", e, file=sys.stderr)
        return ""


def puppeteer_config() -> str:
    """mermaid-cli drives its own Chromium: reuse Playwright's so nothing extra is downloaded; --no-sandbox for root."""
    cfg: dict = {"args": ["--no-sandbox", "--disable-gpu"]} if os.geteuid() == 0 else {}
    for base in (os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), Path.home() / ".cache/ms-playwright", Path.home() / "Library/Caches/ms-playwright"):
        if base:
            hits = sorted(Path(base).glob("chromium-*/chrome-linux/chrome")) + sorted(Path(base).glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"))
            if hits:
                cfg["executablePath"] = str(hits[-1])
                break
    f = WORK / "puppeteer.json"
    f.write_text(json.dumps(cfg))
    return str(f)


def render_diagrams(force: bool = False) -> None:
    """docs/1.0/diagrams/*.mmd -> .work/diagrams/*.svg with the app theme; wide LR flowcharts become TB."""
    d = WORK / "diagrams"
    d.mkdir(exist_ok=True)
    for src in sorted((DOCS / "diagrams").glob("*.mmd")):
        out = d / (src.stem + ".svg")
        if out.exists() and not force and out.stat().st_mtime > src.stat().st_mtime:
            continue
        text = src.read_text()
        if src.stem in ("concepts-pipeline", "host-setup", "deploy-azure"):
            text = text.replace("flowchart LR", "flowchart TB", 1)
        tmp = d / src.name
        tmp.write_text(text)
        cmd = ["npx", "-y", "@mermaid-js/mermaid-cli@11", "-c", str(HERE / "mermaid-theme.json"), "-i", str(tmp), "-o", str(out), "-b", "transparent", "-p", puppeteer_config()]
        print("  mermaid", src.name)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode or not out.exists():
            print("  ! mermaid failed for", src.name, r.stderr[-400:], file=sys.stderr)
            fallback = src.with_suffix(".svg")
            if fallback.exists():
                shutil.copy(fallback, out)


def take_shots(force: bool = False) -> None:
    if SHOTS.exists() and any(SHOTS.glob("*.png")) and not force:
        return
    SHOTS.mkdir(parents=True, exist_ok=True)
    print("  screenshots (vite mock mode)")
    subprocess.run([sys.executable, str(HERE / "shots.py"), str(SHOTS), VERSION], check=True, cwd=ROOT / "frontend")


CSS_BODY = """
:root{--nav:#0b1f3a;--nav2:#142d50;--navb:#1f3759;--navt:#c5d2e4;--navm:#768cac;--accent:#22d3ee;--primary:#1b6ef3;--primary-soft:#e8f0fe;
--primary-text:#155cd0;--bg:#f4f6f9;--surface:#fff;--surface2:#f8fafc;--border:#e1e6ed;--border2:#cbd3de;--text:#162030;--muted:#627086;
--hint-bg:#eef5ff;--hint-border:#bfd6fc;--hint-text:#173770;}
@page{size:A4;margin:18mm 16mm 20mm 16mm}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;font-size:10.2pt;line-height:1.5;color:var(--text);-webkit-print-color-adjust:exact;print-color-adjust:exact}
code,pre,kbd,.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
a{color:var(--primary-text);text-decoration:none}
/* ---------- cover ---------- */
.cover{page:cover;break-after:page;height:257mm;margin:-18mm -16mm -20mm -16mm;padding:26mm 22mm;background:linear-gradient(160deg,#0e2a4f 0%,#0b1f3a 55%,#071a33 100%);color:#fff;position:relative;overflow:hidden}
.cover .mesh{position:absolute;inset:0;opacity:.55}
.cover .logo{width:120mm}
.cover h1{font-size:40pt;font-weight:800;letter-spacing:-1.2pt;margin:30mm 0 4mm;line-height:1.05}
.cover h1 span{color:var(--accent)}
.cover .sub{font-size:15pt;color:var(--navt);max-width:120mm;line-height:1.4}
.cover .meta{position:absolute;left:22mm;right:22mm;bottom:22mm;display:flex;justify-content:space-between;align-items:flex-end;border-top:1px solid var(--navb);padding-top:6mm;font-size:9.5pt;color:var(--navt)}
.cover .pill{display:inline-block;background:var(--nav2);border:1px solid var(--navb);border-radius:999px;padding:2px 10px;color:#fff;font-weight:600;font-size:9pt;margin-right:6px}
.cover .chips{margin-top:12mm;display:flex;flex-wrap:wrap;gap:6px}
.cover .chip{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.06);border:1px solid var(--navb);border-radius:8px;padding:5px 10px;font-size:9pt;color:#e6eefb}
.cover .chip svg{width:13px;height:13px;color:var(--accent)}
/* ---------- toc ---------- */
.toc{break-after:page}
.toc h2{font-size:20pt;font-weight:800;letter-spacing:-.4pt;margin:0 0 8mm}
.toc .ch{display:flex;align-items:center;gap:10px;margin-top:5mm;padding:7px 10px;background:var(--nav);color:#fff;border-radius:8px;font-weight:700;font-size:11pt}
.toc .ch .ico{width:26px;height:26px;border-radius:7px;background:var(--nav2);display:flex;align-items:center;justify-content:center;color:var(--accent)}
.toc .ch .ico svg{width:15px;height:15px}
.toc .ch .no{color:var(--navm);font-weight:600;font-size:9pt;letter-spacing:1px}
.toc .ch .pg,.toc .sec .pg{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}
.toc .sec{display:flex;align-items:baseline;padding:2px 10px 2px 46px;font-size:9.6pt;color:var(--text)}
.toc .sec .t{flex:0 1 auto}
.toc .sec .dots{flex:1 1 auto;border-bottom:1px dotted var(--border2);margin:0 6px;transform:translateY(-3px)}
.toc .sec .pg{color:var(--muted);font-weight:500}
/* ---------- chapter opener ---------- */
.chapter{break-before:page}
.opener{margin:0 -16mm 8mm -16mm;border-radius:0 0 10px 10px;padding:14mm 16mm 10mm;background:linear-gradient(135deg,#0e2a4f,#0b1f3a 60%,#071a33);color:#fff;display:flex;gap:7mm;align-items:center}
.opener .ico{flex:none;width:22mm;height:22mm;border-radius:6mm;background:var(--nav2);border:1px solid var(--navb);display:flex;align-items:center;justify-content:center;color:var(--accent)}
.opener .ico svg{width:12mm;height:12mm}
.opener .no{font-size:9pt;letter-spacing:2.5px;text-transform:uppercase;color:var(--navm);font-weight:700}
.opener h1{margin:1mm 0 2mm;font-size:26pt;font-weight:800;letter-spacing:-.6pt;line-height:1.05}
.opener .blurb{color:var(--navt);font-size:10.5pt;max-width:140mm}
/* ---------- body ---------- */
.body h1{display:none}
.body h2{font-size:16pt;font-weight:800;letter-spacing:-.3pt;margin:9mm 0 3mm;padding-bottom:2mm;border-bottom:2px solid var(--primary);break-after:avoid;color:var(--nav)}
.body h3{font-size:12pt;font-weight:700;margin:6mm 0 2mm;break-after:avoid;color:var(--nav)}
.body h4{font-size:10.5pt;font-weight:700;margin:4mm 0 1.5mm;break-after:avoid;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
.body p{margin:0 0 2.6mm}
.body ul{margin:0 0 3mm;padding-left:5.5mm}.body ol{margin:0 0 3mm;padding-left:7mm}
.body li{margin:0 0 1mm}
.body li>p{margin:0}
.body strong{font-weight:700;color:var(--nav)}
.body code{background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:0 4px;font-size:8.8pt;color:#0b3b8c}
.body pre{background:#0b1f3a;color:#dbe7fb;border-radius:8px;padding:3.2mm 4mm;font-size:8.3pt;line-height:1.45;white-space:pre-wrap;word-break:break-word;margin:2.5mm 0 4mm;break-inside:avoid;border-left:3px solid var(--accent)}
.body pre code{background:none;border:0;padding:0;color:inherit;font-size:inherit}
.body blockquote{margin:3mm 0;padding:2.5mm 4mm;background:var(--hint-bg);border:1px solid var(--hint-border);border-left:3px solid var(--primary);border-radius:6px;color:var(--hint-text)}
.body blockquote p{margin:0}
.body table{width:100%;border-collapse:collapse;margin:2.5mm 0 5mm;font-size:8.8pt;break-inside:auto}
.body th{text-align:left;background:var(--surface2);color:var(--muted);font-size:7.6pt;text-transform:uppercase;letter-spacing:.6px;font-weight:700;padding:5px 7px;border-bottom:1px solid var(--border2)}
.body td{padding:4px 7px;border-bottom:1px solid var(--border);vertical-align:top}
.body tr{break-inside:avoid}
.body td code{font-size:8pt}
.body hr{border:0;border-top:1px solid var(--border);margin:5mm 0}
.body img.diagram{display:block;max-width:100%;max-height:235mm;width:auto;height:auto;margin:3mm auto 1.5mm}
figure{margin:4mm 0 6mm;break-inside:avoid}
figure.diagram{border:1px solid var(--border);border-radius:10px;padding:4mm;background:var(--surface)}
@page wide{size:A4 landscape}
figure.wide{page:wide;margin:0;border:0;padding:0}
figure.wide img.diagram{max-height:150mm}
figure.shot{border:1px solid var(--border2);border-radius:10px;overflow:hidden;background:var(--surface);box-shadow:0 4px 14px rgba(11,31,58,.08)}
figure.shot .bar{height:7mm;background:var(--surface2);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:5px;padding:0 3mm}
figure.shot .bar i{display:block;width:8px;height:8px;border-radius:50%;background:var(--border2)}
figure.shot .bar span{margin-left:auto;font-size:7.5pt;color:var(--muted);font-family:'JetBrains Mono'}
figure.shot img{display:block;width:100%;height:auto}
figcaption{font-size:8.6pt;color:var(--muted);padding:2mm 3mm 0;text-align:center}
figure.shot figcaption{padding-bottom:2.5mm;background:var(--surface);border-top:1px solid var(--border);text-align:left}
.beta{display:inline-block;background:#fff4d6;color:#8a5a00;border:1px solid #f5d58a;border-radius:999px;font-size:7.5pt;padding:0 7px;font-weight:700;vertical-align:middle;letter-spacing:.3px}
.tag{display:inline-block;background:var(--primary-soft);color:var(--primary-text);border-radius:999px;font-size:7.5pt;padding:0 7px;font-weight:700;vertical-align:middle}
.colophon{break-before:page;height:230mm;display:flex;flex-direction:column;justify-content:flex-end;color:var(--muted);font-size:9pt}
.colophon .mark{width:18mm;margin-bottom:4mm}
"""


def icon(name: str, cls: str = "") -> str:
    d = ICONS.get(name, ICONS["info"])
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" '
            f'stroke-linecap="round" stroke-linejoin="round"><path d="{d}"/></svg>')


def md_to_html(src: str, fname: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Return (html, headings[(level, text, anchor)]) for one chapter."""
    # drop mermaid source blocks (the rendered SVG follows them)
    src = re.sub(r"```mermaid\n.*?\n```\n?", "", src, flags=re.S)
    # chapter cross-links -> anchors
    src = re.sub(r"\]\((0\d)-[a-z-]+\.md(#[^)]*)?\)", lambda m: f"](#ch-{int(m.group(1))})", src)
    src = re.sub(r"\]\(README\.md\)", "](#ch-0)", src)
    src = src.replace("](../", "](https://github.com/avdlab/")  # repo-relative links become plain references
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"], extension_configs={"toc": {"anchorlink": False, "permalink": False}})
    out = md.convert(src)
    headings = [(int(t["level"]), html.unescape(re.sub("<[^>]+>", "", t["name"])), t["id"]) for t in flatten(md.toc_tokens)]
    # diagrams
    def fig(m):
        name, w = m.group(2), svg_width(WORK / "diagrams" / m.group(2))
        cls = "diagram wide" if w > 1600 else "diagram"
        return f'<figure class="{cls}"><img class="diagram" src="file://{WORK}/diagrams/{name}" alt="{m.group(1)}"><figcaption>{m.group(1)}</figcaption></figure>'
    out = re.sub(r'<p><img alt="([^"]*)" src="diagrams/([^"]+)"\s*/?></p>', fig, out)
    # a landscape figure straight after a heading would leave that page empty: let the first paragraph(s) go first
    out = re.sub(r'(<h[23][^>]*>[^\n]*?</h[23]>\n?)(<figure class="diagram wide">.*?</figure>)(\n?(?:<p>.*?</p>\n?|<ul>.*?</ul>\n?){1,3})', r"\1\3\2", out, flags=re.S)
    # badges
    out = re.sub(r"<em>(Beta|beta)</em>", '<span class="beta">BETA</span>', out)
    out = re.sub(r"\(beta\)", '<span class="beta">BETA</span>', out)
    out = re.sub(r"<em>(Preview|preview)</em>", '<span class="tag">PREVIEW</span>', out)
    # screenshots
    for key, shots in SHOT_MAP.get(fname, {}).items():
        figs = "".join(shot_fig(n, cap) for n, cap in shots if (SHOTS / f"{n}.png").exists())
        pat = re.compile(r'(<h[23] id="[^"]*">' + re.escape(html.escape(key)) + r'[^\n]*?</h[23]>)')
        out, n = pat.subn(lambda m: m.group(1) + figs, out, count=1)
        if not n:
            print("  ! no heading for screenshot key", key, file=sys.stderr)
    return out, headings


def svg_width(p: Path) -> float:
    m = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', p.read_text())
    return float(m.group(3)) if m else 0


def flatten(tokens):
    for t in tokens:
        yield t
        yield from flatten(t.get("children", []))


def shot_fig(name: str, caption: str) -> str:
    return (f'<figure class="shot"><div class="bar"><i></i><i></i><i></i><span>avdlab · {name}</span></div>'
            f'<img src="file://{SHOTS}/{name}.png" alt="{html.escape(caption)}"><figcaption>{html.escape(caption)}</figcaption></figure>')


def build_html(pages: dict[str, int] | None) -> tuple[str, list]:
    chapters = []
    for fname, no, title, ico, blurb in CHAPTERS:
        body, heads = md_to_html((DOCS / fname).read_text(), fname)
        chapters.append((fname, no, title, ico, blurb, body, heads))

    def pg(anchor: str) -> str:
        return str(pages.get(anchor, "")) if pages else ""

    chips = "".join(f'<span class="chip">{icon(i)}{t}</span>' for i, t in [("labs", "AVD 6.4 examples as templates"), ("server", "containerlab + cEOS on Linux, Mac or Windows hosts"), ("check", "ANTA 1.10 validation and PSIRT"), ("cloud", "CloudVision, NetBox, ACT"), ("feedback", "Bring-your-own-model AI designer"), ("users", "Multi-user, roles, TOTP MFA")])
    mesh = '<svg class="mesh" viewBox="0 0 600 850" preserveAspectRatio="none"><g stroke="#22d3ee" stroke-width="1" opacity=".35">' + "".join(
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>' for x1, y1, x2, y2 in [(380, 520, 300, 700), (380, 520, 460, 700), (380, 520, 620, 700), (540, 520, 300, 700), (540, 520, 460, 700), (540, 520, 620, 700), (300, 700, 320, 820), (460, 700, 440, 820), (620, 700, 600, 820)]) + \
        '</g><g fill="#0b2545" stroke="#67e8f9" stroke-width="2">' + "".join(f'<circle cx="{x}" cy="{y}" r="9"/>' for x, y in [(380, 520), (540, 520), (300, 700), (460, 700), (620, 700)]) + "</g></svg>"
    cover = f"""<section class="cover">{mesh}<div class="logo">{LOGO}</div>
<h1>Documentation<br><span>release {VERSION.rsplit(".", 1)[0]}</span></h1>
<div class="sub">The web-administered Arista AVD lab orchestrator: templates → knobs → build → deploy → validate → CloudVision, with the architecture, schematics and a page-by-page guide.</div>
<div class="chips">{chips}</div>
<div class="meta"><div><span class="pill">v{VERSION}</span><span class="pill">{TODAY}</span></div><div style="text-align:right">Dietrich De Bruyne<br>Systems Engineer, Arista Networks</div></div></section>"""

    toc = ['<section class="toc"><h2>Contents</h2>']
    for fname, no, title, ico, blurb, body, heads in chapters:
        toc.append(f'<div class="ch"><span class="ico">{icon(ico)}</span><span class="no">Ch. {no}</span><span>{title}</span><span class="pg">{pg(f"ch-{no}")}</span></div>')
        for lvl, text, anchor in heads:
            if lvl == 2:
                toc.append(f'<div class="sec"><span class="t">{html.escape(text)}</span><span class="dots"></span><span class="pg">{pg(anchor)}</span></div>')
    toc.append("</section>")

    parts = ["".join(toc)]
    for fname, no, title, ico, blurb, body, heads in chapters:
        parts.append(f'<section class="chapter" id="ch-{no}"><div class="opener"><div class="ico">{icon(ico)}</div><div><div class="no">Chapter {no}</div><h1>{title}</h1><div class="blurb">{blurb}</div></div></div><div class="body">{body}</div></section>')
    parts.append(f'<section class="colophon"><div class="mark">{MARK}</div><div><b>avdlab {VERSION}</b> — documentation generated {TODAY} from <code>docs/1.0/</code> and the application source.<br>© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks. Arista, EOS, CloudVision and AVD are trademarks of Arista Networks. containerlab, ANTA and NetBox belong to their respective projects.</div></section>')
    head = f'<!doctype html><html><head><meta charset="utf-8"><title>avdlab {VERSION} documentation</title><style>{CSS}</style></head><body>'
    doc = head + "".join(parts) + "</body></html>"
    (WORK / "cover.html").write_text(head.replace("@page{size:A4;margin:18mm 16mm 20mm 16mm}", "@page{size:A4;margin:0}") + cover.replace('class="cover"', 'class="cover" style="margin:0;height:297mm;break-after:auto"') + "</body></html>")
    anchors = []
    for _, no, _, _, _, _, heads in chapters:
        anchors.append(f"ch-{no}")
        anchors += [a for lvl, _, a in heads if lvl == 2]
    titles = {f"ch-{no}": f"CHAPTER {no} " for _, no, title, *_ in CHAPTERS} | {a: t for *_, heads in chapters for lvl, t, a in heads if lvl == 2}
    return doc, [(a, titles[a]) for a in anchors]


HEADER = f"""<div style="font-family:Inter,sans-serif;font-size:7.5pt;color:#627086;width:100%;padding:0 16mm;display:flex;justify-content:space-between;align-items:center">
<span style="font-weight:700;color:#0b1f3a">avd<span style="color:#1b6ef3">lab</span> <span style="font-weight:500;color:#627086">documentation · v{VERSION}</span></span><span></span></div>"""
FOOTER = """<div style="font-family:Inter,sans-serif;font-size:7.5pt;color:#627086;width:100%;padding:0 16mm;display:flex;justify-content:space-between">
<span>© 2026 Dietrich De Bruyne — Systems Engineer, Arista Networks</span><span>page <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>"""


async def render(doc: str, out: Path, cover: bool = True) -> None:
    (WORK / "doc.html").write_text(doc)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        await pg.goto(f"file://{WORK}/doc.html", wait_until="load")
        await pg.evaluate("document.fonts.ready")
        await pg.wait_for_timeout(800)
        await pg.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True, display_header_footer=True,
                     header_template=HEADER, footer_template=FOOTER, margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"})
        if cover:
            await pg.goto(f"file://{WORK}/cover.html", wait_until="load")
            await pg.evaluate("document.fonts.ready")
            await pg.wait_for_timeout(500)
            await pg.pdf(path=str(WORK / "cover.pdf"), format="A4", print_background=True, prefer_css_page_size=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        await b.close()
    if cover:
        w = PdfWriter()
        for f in (WORK / "cover.pdf", out):
            for page in PdfReader(str(f)).pages:
                w.add_page(page)
        with open(out, "wb") as fh:
            w.write(fh)


def find_pages(pdf: Path, anchors: list[tuple[str, str]]) -> dict[str, int]:
    """Locate the page of each heading: first page (after the TOC) whose text contains the heading text."""
    raw = subprocess.run(["pdftotext", str(pdf), "-"], check=True, capture_output=True, text=True).stdout
    texts = [re.sub(r"\s+", " ", t) for t in raw.split("\f")]
    found: dict[str, int] = {}
    start = 1
    for a, t in anchors:
        key = re.sub(r"\s+", " ", t).strip()
        key = key[:48].replace("(beta)", "BETA")
        hit = None
        for i in range(start, len(texts)):
            if key in texts[i]:
                hit = i + 1
                break
        if hit:
            found[a] = hit
            start = i
            if a.startswith("ch-"):
                # the opener bleeds into the top margin, so pdftotext may file it under the previous page
                found[a] = hit + 1 if "CHAPTER" not in texts[i][:120] and i + 1 < len(texts) else hit
        elif a.startswith("ch-"):
            print("  ! chapter not located:", t, file=sys.stderr)
        else:
            print("  ! heading not located:", t, file=sys.stderr)
    # a chapter starts on the page of its first section (the opener is a quarter page)
    last = None
    for a, _ in anchors:
        if a.startswith("ch-"):
            last = a
        elif last and a in found:
            found[last] = min(found.get(last, 10**6), found[a]); last = None
    return found


async def main() -> None:
    global CSS, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--skip-shots", action="store_true", help="do not (re)take screenshots; use what is in .work/shots")
    ap.add_argument("--skip-diagrams", action="store_true", help="do not re-render diagrams")
    ap.add_argument("--force", action="store_true", help="re-render diagrams and retake screenshots")
    a = ap.parse_args()
    OUT = a.out
    OUT.parent.mkdir(parents=True, exist_ok=True)
    CSS = fonts_css() + CSS_BODY
    if not a.skip_diagrams:
        render_diagrams(a.force)
    if not a.skip_shots:
        take_shots(a.force)
    doc, anchors = build_html(None)
    tmp = WORK / "pass1.pdf"
    await render(doc, tmp, cover=False); print("pass1 done", flush=True)
    pages = find_pages(tmp, anchors); print("pages located", len(pages), flush=True)
    doc, _ = build_html(pages)
    await render(doc, OUT)
    print("pages:", len(PdfReader(str(OUT)).pages))
    print("wrote", OUT)


if __name__ == "__main__":
    asyncio.run(main())
