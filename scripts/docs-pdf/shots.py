"""Screenshots of the SPA in mock mode for the documentation PDF.  Usage: shots.py <out-dir> <version-label>
Starts `vite --port 5199` with VITE_MOCK=1 in frontend/ unless something already answers there."""
import asyncio, os, pathlib, subprocess, sys, time, urllib.request
from playwright.async_api import async_playwright
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "shots"); OUT.mkdir(parents=True, exist_ok=True)
VER = sys.argv[2] if len(sys.argv) > 2 else "1.0.0"
B="http://localhost:5199"
HIDE="""(()=>{const V='""" + VER + """';const c=document.createElement('style');c.textContent='[data-testid=update-pill]{display:none!important}';document.head.appendChild(c);
const h=()=>{document.querySelectorAll('aside span').forEach(e=>{if(e.textContent.trim().toLowerCase()==='mock')e.style.display='none'});document.querySelectorAll('[data-testid=version-badge]').forEach(e=>{if(!e.textContent.includes(V))e.textContent='v'+V});document.querySelectorAll('footer, .footer').forEach(f=>{if(/v0\.6\.0/.test(f.innerHTML))f.innerHTML=f.innerHTML.replace(/v0\.6\.0/g,'v'+V)})};h();new MutationObserver(h).observe(document.body,{subtree:true,childList:true})})()"""
async def nav(pg, path, settle=1600):
    await pg.evaluate("p => { history.pushState({}, '', p); dispatchEvent(new PopStateEvent('popstate')) }", path); await pg.wait_for_timeout(settle)
async def shot(pg, name, full=False, clip=None):
    await pg.screenshot(path=str(OUT / f"{name}.png"), full_page=full, clip=clip)
def ensure_vite():
    try:
        urllib.request.urlopen(B, timeout=2); return None
    except Exception:
        pass
    env = dict(os.environ, VITE_MOCK="1")
    proc = subprocess.Popen(["npx", "vite", "--port", "5199", "--strictPort"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        time.sleep(1)
        try:
            urllib.request.urlopen(B, timeout=2); return proc
        except Exception:
            continue
    proc.terminate(); raise SystemExit("vite did not start on :5199")
async def main():
    proc = ensure_vite()
    async with async_playwright() as p:
        b=await p.chromium.launch(); ctx=await b.new_context(viewport={"width":1440,"height":900}, device_scale_factor=2); pg=await ctx.new_page()
        await pg.goto(B+"/login"); await pg.wait_for_timeout(1200); await pg.evaluate(HIDE)
        await shot(pg,"login")
        await pg.fill("input[autocomplete=username]","admin"); await pg.fill("input[type=password]","demo"); await pg.click("form button"); await pg.wait_for_timeout(1800); await pg.evaluate(HIDE)
        await shot(pg,"dashboard")
        await nav(pg,"/getting-started"); await pg.hover("[data-testid=gs-step-build]"); await pg.wait_for_timeout(300); await shot(pg,"getting-started", full=True)
        await nav(pg,"/hosts"); await shot(pg,"hosts")
        await pg.click("text=Add host"); await pg.wait_for_timeout(500); await pg.click("[data-testid=platform-mac]"); await pg.wait_for_timeout(300); await shot(pg,"add-host")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(300)
        labs = await pg.evaluate("fetch('/api/labs').then(r=>r.json())")
        hid = next(l for l in labs if l['name']=='evpn-lab')['id']
        await nav(pg,"/hosts/host-lab1"); await pg.wait_for_timeout(1200); await shot(pg,"host-detail")
        await nav(pg,"/images"); await shot(pg,"images")
        await nav(pg,"/labs"); await shot(pg,"labs")
        await nav(pg,"/labs/new"); await shot(pg,"new-lab")
        await pg.click("[data-testid=template-single-dc-l3ls] button:has-text('Use')"); await pg.wait_for_timeout(1200); await shot(pg,"new-lab-knobs")
        await nav(pg,"/labs"); await pg.click("a:has-text('evpn-lab')"); await pg.wait_for_timeout(1200); await shot(pg,"lab-overview")
        for tab in ["Build","Topology","Runtime","Devices","Clients","Validation","CloudVision","Files"]:
            await pg.click(f"[role=tab]:has-text('{tab}')"); await pg.wait_for_timeout(2200); await shot(pg, f"lab-{tab.lower()}")
        await pg.click("[role=tab]:has-text('Validation')"); await pg.wait_for_timeout(1000)
        card = await pg.query_selector("[data-testid=psirt-card]")
        if card:
            await card.scroll_into_view_if_needed(); await pg.wait_for_timeout(300); await shot(pg,"lab-psirt")
        await nav(pg,"/learn"); await shot(pg,"learn")
        await nav(pg,"/cloudvision"); await shot(pg,"cloudvision")
        await nav(pg,"/users"); await shot(pg,"users")
        await nav(pg,"/admin/settings"); await shot(pg,"admin-settings")
        await nav(pg,"/account"); await shot(pg,"account")
        await b.close()
    if proc:
        proc.terminate()
asyncio.run(main())
