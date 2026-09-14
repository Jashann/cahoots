"""Record the demo scenes from the live app with Playwright (offscreen, 1280x720), then video/mux.py fits
each scene to its narration and muxes. Everything on screen is the live deployment.

  PYTHONPATH=. .venv/bin/python video/record.py --base https://98-93-7-42.sslip.io --pid u-xxxx --key KEY --memory /tmp/sample_memory.txt [--only s4_scheme]

Scenes (ids match video/narration.py):
  s1_text  the thread page          s2_idea   title card, then /network zoomed
  s3_card  onboarding -> card -> approve (a real card extraction on screen)
  s4_scheme the agent's notes (admin folder view), then /network threads with captions (video/spots.json)
  s5_invite the one screen in a phone frame: the question, a tap, then talking to the agent
  s6_arch  docs/scenes/arch_slide.html lit card by card, then /agents      s7_close docs/scenes/close.html
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"
W, H = 1280, 720
CAP = """(()=>{const c=document.createElement('div'); c.id='cap'; c.style.cssText='position:fixed;left:0;right:0;bottom:0;padding:14px 26px;background:rgba(26,26,26,.92);color:#fff;font:700 26px/1.3 -apple-system,Segoe UI,Helvetica,Arial;z-index:9999'; document.body.appendChild(c);})()"""


def _scene(pw, name: str, fn, size=(W, H)):
    d = OUT / "raw" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    b = pw.chromium.launch()
    w, h = int(size[0] * 1.5), int(size[1] * 1.5)  # the layouts are designed at 1280x720; recorded at 1.5x (1920x1080) with every top-level page zoomed 1.5
    ctx = b.new_context(viewport={"width": w, "height": h}, record_video_dir=str(d), record_video_size={"width": w, "height": h}, device_scale_factor=1)
    ctx.add_init_script("if (window === window.top) { document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.zoom = '1.5'; }); }")
    pg = ctx.new_page()
    SCENE["start"] = time.time(); SCENE["lead"] = 0.0; SCENE["marks"] = []
    try:
        fn(pg)
    finally:
        ctx.close()
        b.close()
    vids = list(d.glob("*.webm"))
    vids[0].rename(OUT / f"{name}.webm")
    # the recording began at new_page(); the scene's clock began after goto and the settle wait. Cut that lead so film time 0 is cue time 0,
    # then any wait the scene marked (the card extractor) on top of it.
    cuts = json.loads((OUT / "cuts.json").read_text()) if (OUT / "cuts.json").exists() else {}
    cuts[name] = [("cut", 0.0), ("resume", round(SCENE["lead"], 3))] + [(k, round(t + SCENE["lead"], 3)) for k, t in SCENE["marks"]]  # marks are clock time; the film is cut in recording time
    (OUT / "cuts.json").write_text(json.dumps(cuts, indent=1))
    print("recorded", name, f"(lead {SCENE['lead']:.2f}s)")


SCENE: dict = {"start": 0.0, "lead": 0.0, "marks": []}



CUES = json.loads((OUT / "cues.json").read_text()) if (OUT / "cues.json").exists() else {}

DIM = """(()=>{let s=document.getElementById('dimcss'); if(!s){s=document.createElement('style');s.id='dimcss';
  s.textContent='.vdim{opacity:.18!important;transition:opacity .5s} .vlit{outline:3px solid #c8501f;outline-offset:5px;border-radius:12px;transition:outline-color .4s}';
  document.head.appendChild(s);}})()"""


def clock(pg):
    """hold(t): wait until t seconds of FILM have passed since this scene started, so every shot lands on its cue.
    A wait that mux.py cuts out of the film (the card extractor, an agent's answer) is skipped with hold.skip(cut, resume),
    so the cues after it stay on film time rather than wall time; without that the whole tail of a scene fired at once
    and the film padded the last frame for the length of the wait."""
    t0 = time.time()
    SCENE["lead"] = t0 - SCENE["start"]  # seconds of recording before cue 0; mux cuts them
    state = {"shift": 0.0}

    def hold(t):
        dt = t + state["shift"] - (time.time() - t0)
        if dt > 0:
            pg.wait_for_timeout(int(dt * 1000))
    hold.now = lambda: time.time() - t0
    hold.skip = lambda cut, resume: state.__setitem__("shift", state["shift"] + (resume - cut))
    return hold


def light(pg, needle: str, dim_rest: bool = True):
    """Outline the smallest element containing this text and fade the rest of the page."""
    pg.evaluate(DIM)
    pg.evaluate("""(n)=>{document.querySelectorAll('.vlit,.vdim').forEach(e=>e.classList.remove('vlit','vdim'));
      const cands=[...document.querySelectorAll('main *, body *')].filter(e=>e.children.length<12 && (e.innerText||'').includes(n));
      const el=cands.sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length)[0];
      if(!el) return false;
      el.classList.add('vlit');
      [...document.querySelectorAll('main > *, body > *')].forEach(x=>{ if(!x.contains(el) && x.id!=='cap') x.classList.add('vdim'); });
      el.scrollIntoView({block:'center'});
      return true;}""", needle)


def undim(pg):
    pg.evaluate("document.querySelectorAll('.vdim').forEach(e=>e.classList.remove('vdim'));document.querySelectorAll('.vlit').forEach(e=>e.classList.remove('vlit'))")


def caption(pg, text: str):
    pg.evaluate("""(c)=>{let e=document.getElementById('cap'); if(!e){e=document.createElement('div'); e.id='cap';
        e.style.cssText='position:fixed;left:0;right:0;bottom:0;padding:14px 26px;background:rgba(26,26,26,.92);color:#fff;font:700 26px/1.3 -apple-system,Segoe UI,Helvetica,Arial;z-index:9999';
        document.body.appendChild(e);} e.textContent=c}""", text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--pid", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--memory", default="/tmp/sample_memory.txt")
    ap.add_argument("--only", default=None)
    ap.add_argument("--real", default="", help="a real person's screen URL (with key) for the real-people beat")
    ap.add_argument("--thread", default="t-299d5a", help="the thread id between two real people's agents")
    a = ap.parse_args()
    memory = Path(a.memory).read_text()
    durs = json.loads((OUT / "durations.json").read_text()) if (OUT / "durations.json").exists() else {}
    live = OUT / "live_user.json"
    if not a.pid and live.exists():
        a.pid, a.key = json.loads(live.read_text())["pid"], json.loads(live.read_text())["key"]
    me_url = f"{a.base}/me/{a.pid}?k={a.key}"

    def s1(pg):  # the page's own timers start on load: the clock starts there too, so the lead before it is cut
        pg.goto((ROOT / "docs/scenes/text_thread.html").as_uri()); hold = clock(pg); hold(durs["s1_text"] + 0.5)

    def s2(pg):  # the argument, drawn: the bottleneck, the room, the numbers, hiring, what can't be published, then everyone gets an agent
        pg.goto((ROOT / "docs/scenes/bottleneck.html").as_uri()); hold = clock(pg); hold(durs["s2_idea"] + 0.6)

    def s3(pg):  # what your AI already knows -> the card it draws from it, cut to the cues in video/out/cues.json
        pg.goto(a.base); pg.wait_for_timeout(1300)
        pg.evaluate("""([n,h,m])=>{
            const set=(sel,v)=>{const e=document.querySelector(sel);
              const P=e.tagName==='TEXTAREA'?window.HTMLTextAreaElement:window.HTMLInputElement;
              Object.getOwnPropertyDescriptor(P.prototype,'value').set.call(e,v);
              e.dispatchEvent(new Event('input',{bubbles:true}));};
            set('input[name=first_name]',n); set('input[name=neighbourhood]',h); set('textarea[name=memory]',m);
            const t=document.querySelector('textarea[name=memory]');
            t.style.cssText+=';height:340px;font-size:16px;line-height:1.65'; t.scrollTop=0; t.scrollIntoView({block:'center'});
        }""", ["Sam", "Wolseley", memory])
        hold = clock(pg); marks = []
        caption(pg, "What your AI already remembers about you")
        hold(2.58)                                    # months of conversations: read down it
        pg.evaluate("const t=document.querySelector('textarea[name=memory]'); window.__sc=setInterval(()=>{t.scrollTop+=1.3},40)")
        hold(8.40); pg.evaluate("clearInterval(window.__sc)")
        hold(8.88)                                    # you hand that memory over once
        caption(pg, "Handed over once"); pg.wait_for_timeout(500)
        pg.evaluate("document.getElementById('cap')?.remove()")
        pg.click("button[type=submit]"); cut = hold.now(); marks.append(("cut", cut))
        pg.wait_for_url("**/card/**", timeout=240000)
        pg.wait_for_selector("input[name=one_liner]", timeout=240000)
        resume = hold.now() - 0.3; marks.append(("resume", resume)); hold.skip(cut, resume)
        pg.wait_for_timeout(700)
        pg.evaluate("""(m)=>{const d=document.createElement('div'); d.id='mem';
            d.style.cssText='position:fixed;right:18px;top:70px;width:330px;max-height:560px;overflow:hidden;background:#1b1b1f;color:#cfc9c0;border:1px solid #33333a;border-radius:12px;padding:14px 16px;font:14px/1.6 -apple-system,Segoe UI,Helvetica,Arial;z-index:9998';
            d.innerHTML='<div style="font:700 11px/1.4 -apple-system;letter-spacing:.12em;text-transform:uppercase;color:#8a8a92;margin-bottom:9px">kept private, never shared</div>'
              + m.split('\\n').filter(x=>x.trim()).map(x=>'<div class="ml" style="margin:7px 0">'+x.replace(/^- /,'')+'</div>').join('');
            document.body.appendChild(d);}""", memory)
        hold(13.71); caption(pg, "It knows all of this. It talks to strangers all day.")
        hold(20.17); pg.evaluate("document.getElementById('cap')?.remove()")
        light(pg, "One line, how others will see you")   # it decides what it is allowed to share
        hold(24.30); caption(pg, "Everything you pasted stays in its private files. Out of it, one short card.")
        hold(29.98); caption(pg, "The card is the only thing it publishes")
        hold(32.91); caption(pg, "What you ask it to find goes only to the agents it writes to")
        hold(37.49)
        pg.evaluate("document.getElementById('cap')?.remove()"); undim(pg)
        pg.evaluate("""()=>{  // no strikethrough: the whole panel stays private, and striking a few lines would imply the rest reached the card
            document.querySelectorAll('#mem .ml').forEach(e=>{const t=e.textContent.toLowerCase();
                if(t.includes('peanut')||t.includes("family member")){e.style.cssText+=';color:#ff8a66;font-weight:600';}
                else {e.style.cssText+=';opacity:.6';}});
            const h=document.querySelector('#mem div'); if(h) h.textContent='all of this stays in the private files';}""")
        caption(pg, "The card on the left is what leaves. All of this stays private.")
        hold(43.45); pg.evaluate("document.getElementById('cap')?.remove(); document.getElementById('mem')?.remove()")
        pg.click("button[type=submit]"); pg.wait_for_url("**/me/**", timeout=90000)
        u = pg.url
        pid, key = u.split("/me/")[1].split("?")[0], (u.split("k=")[1].split("&")[0] if "k=" in u else "")
        live.write_text(json.dumps({"pid": pid, "key": key, "url": u}))
        SCENE["marks"] = marks  # the extractor wait, cut by mux after the lead
        print("  onboarded", pid, "key", key)
        hold(48.15)

    def s4(pg):  # its own memory, then three minutes of work on the live network
        pg.goto(f"{a.base}/replay"); pg.wait_for_timeout(1200)
        hold = clock(pg)
        caption(pg, "Its own notes: what it read, who it wrote to, what it decided")
        hold(3.56)
        pg.evaluate("window.__s=setInterval(()=>window.scrollBy(0,1),36)")
        hold(8.90); pg.evaluate("clearInterval(window.__s)")
        hold(9.10); pg.evaluate("document.getElementById('cap')?.remove()")
        pg.goto(f"{a.base}/graph?focus={a.pid}&big=1&autoplay=1"); pg.wait_for_timeout(400)
        pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('h1')?.remove(); document.querySelector('.sub')?.remove(); document.querySelector('.views')?.remove()")
        hold(9.51); caption(pg, "Each dot is one agent")
        hold(11.69); caption(pg, "Orange: a real person. Grey: one of the sixty residents we invented.")
        hold(19.75); caption(pg, "Three minutes in: three readers found, without being asked")
        hold(25.90); pg.evaluate("document.getElementById('cap')?.remove()")
        pg.goto(f"{a.base}/network?all=1"); pg.wait_for_timeout(1200)
        pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.45'; document.querySelectorAll('details').forEach(d=>d.open=true)")
        spots = json.loads((HERE / "spots.json").read_text())
        spot = """(()=>{const el=[...document.querySelectorAll('.chat .bub')].find(m=>m.innerText.includes(ARG)); if(!el) return false;
            document.querySelectorAll('.chat .bub').forEach(m=>{m.querySelector('.b').style.outline='';m.style.opacity='0.32'});
            el.querySelector('.b').style.outline='4px solid #c8501f'; el.style.opacity='1'; el.scrollIntoView({block:'center'}); return true;})()"""
        for needle, t, cap in spots:
            hold(t)
            ok = pg.evaluate(spot.replace("ARG", json.dumps(needle)))
            caption(pg, cap)
            print("  spot", "ok " if ok else "MISSING", needle[:46])
        hold(35.96)

    def s5(pg):  # the one screen: one question, one tap
        pg.goto(me_url); pg.wait_for_timeout(1500)
        hold = clock(pg)
        pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.5'")
        light(pg, "Make an exception this once?")
        caption(pg, "Everything the agent did, and this is all Sam sees")
        hold(2.50); caption(pg, "Outside his hours, so it asks instead of deciding on its own")
        hold(11.96); pg.evaluate("document.getElementById('cap')?.remove()"); undim(pg)
        try:
            pg.click("button[value='Yes, this once']", timeout=9000); pg.wait_for_timeout(1800)
            print("  tapped")
        except Exception as e:
            print("  no button to tap:", repr(e)[:90])
        caption(pg, "One tap, and the agent books it")
        hold(14.69); pg.evaluate("document.getElementById('cap')?.remove()")
        light(pg, "Your plans")
        caption(pg, "Afterwards it asks how it went")
        hold(19.25); pg.evaluate("document.getElementById('cap')?.remove()"); undim(pg)

    def s5b(pg):  # the people you need
        pg.goto(me_url); pg.wait_for_timeout(1200)
        pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.4'")
        hold = clock(pg)
        caption(pg, "A night out is just the easiest example")
        hold(2.92); pg.evaluate("document.getElementById('cap')?.remove()")
        pg.goto(f"{a.base}/network?all=1"); pg.wait_for_timeout(1500)
        pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.45'; document.querySelectorAll('details').forEach(d=>d.open=true)")
        light(pg, "starting a clothing label and looking for manufacturers")
        caption(pg, "Sam asked for a manufacturer. His agent searched what people can help with.")
        hold(9.60)
        light(pg, "cut-and-sew studio with small-batch manufacturing")
        caption(pg, "and came back with an introduction to the one who fits")
        hold(14.88); pg.evaluate("document.getElementById('cap')?.remove()"); undim(pg)
        pg.goto(f"{a.base}/agents"); pg.wait_for_timeout(1300)
        pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.25'")
        caption(pg, "The same search finds who is hiring, and who works in your field")
        hold(20.29); pg.evaluate("document.getElementById('cap')?.remove()")

    def s5c(pg):  # two real people's agents
        pg.goto(f"{a.base}/network?all=1"); pg.wait_for_timeout(2000)
        hold = clock(pg)
        pg.evaluate("""(tid)=>{document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.35';
            const d=[...document.querySelectorAll('details')].find(x=>x.innerText.includes(tid));
            if(d){ d.open=true; d.scrollIntoView({block:'start'}); window.scrollBy(0,-14); d.style.background='#fff8f2'; }}""", a.thread)
        caption(pg, "Two of the twenty-five real people who joined")
        hold(6.23); caption(pg, "Dutch: an acreage, and trees to cut")
        hold(9.13); caption(pg, "Marnie: sheep, and their wool")
        hold(11.94); caption(pg, "They had never met")
        hold(13.89); caption(pg, "Their agents found each other in under ten minutes")
        hold(17.05); caption(pg, "They agreed on Tuesday at seven and asked both people")
        hold(20.40); pg.evaluate("document.getElementById('cap')?.remove()")
        if a.real:
            pg.goto(a.real); pg.wait_for_timeout(1200)
            pg.evaluate("document.querySelector('nav')?.remove(); document.querySelector('main').style.zoom='1.45'")
            light(pg, "In")
            caption(pg, "Dutch's own screen: the same question, two buttons")
        hold(22.98); caption(pg, "Neither did any of this work. They meet only if both tap yes.")
        hold(28.21); pg.evaluate("document.getElementById('cap')?.remove()")

    def s6(pg):  # what is underneath, one card per phrase
        pg.goto((ROOT / "docs/scenes/arch_slide.html").as_uri()); pg.wait_for_timeout(400)
        hold = clock(pg)
        pg.evaluate("""()=>{const bs=[...document.querySelectorAll('.b')]; bs.forEach(b=>{b.style.opacity='0';b.style.transition='opacity .7s'});
            document.querySelectorAll('.b p').forEach(p=>p.style.fontSize='21px'); document.querySelectorAll('.b h2').forEach(h=>h.style.fontSize='28px');}""")
        for i, t in enumerate([0.40, 7.00, 11.69]):
            hold(t); pg.evaluate("(i)=>{const b=document.querySelectorAll('.b')[i]; if(b) b.style.opacity='1';}", i)
        hold(19.94)

    def s7(pg):  # the rules, revealed on the line that states them
        pg.goto((ROOT / "docs/scenes/close.html").as_uri()); pg.wait_for_timeout(400)
        hold = clock(pg)
        pg.evaluate("""()=>{document.querySelectorAll('li').forEach(e=>{e.classList.remove('show');e.style.opacity='0';e.style.transition='opacity .6s'});
            const e=document.querySelector('.end'); if(e){e.classList.remove('show');e.style.opacity='0';e.style.transition='opacity .8s';}}""")
        show = "(i)=>{const l=document.querySelectorAll('li')[i]; if(l) l.style.opacity='1';}"
        for i, t in enumerate([1.60, 4.60, 7.30, 8.80, 9.90, 11.40]):  # the card rule and the tap rule land on the words that state them; the rest complete the list without dead air
            hold(t); pg.evaluate(show, i)
        hold(19.62); pg.evaluate("const e=document.querySelector('.end'); if(e) e.style.opacity='1';")
        hold(24.55)

    scenes = {"s1_text": s1, "s2_idea": s2, "s3_card": s3, "s4_scheme": s4, "s5_invite": s5, "s5b_ask": s5b, "s5c_real": s5c, "s6_arch": s6, "s7_close": s7}
    with sync_playwright() as pw:
        for name, fn in scenes.items():
            if a.only and name != a.only:
                continue
            t = time.time(); _scene(pw, name, fn, size=(960, 540) if name == "s5_invite" else (W, H)); print(f"  {time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
