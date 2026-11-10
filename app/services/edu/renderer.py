"""Render one scene (a validated Visual + narration timing) to a video clip.

How it works
------------
1. Build a self-contained HTML page for the scene. Every item that should
   appear later carries `data-at="<seconds>"`; a tiny script (`renderAt(t)`)
   sets the exact visual state for any time `t`. Nothing relies on real-time
   playback, so output is deterministic.
2. With headless Chromium (Playwright) take screenshots only at *key times*:
   densely (24 fps) while something is animating, once while the screen is
   static. A 15-second scene needs ~100-200 screenshots instead of 360.
3. ffmpeg stitches the screenshots using per-frame durations and muxes the
   narration audio.

All text coming from the LLM is HTML-escaped and network access is blocked
inside the page, so a hostile or malformed plan cannot load anything or run
script.
"""

from __future__ import annotations

import functools
import html
import json
import math
import re
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from app.services.edu.schema import Item, Scene, Visual
from app.services.edu.timeline import Timeline

WIDTH, HEIGHT = 1920, 1080
FPS = 24
ANIM_SECONDS = 0.55

# One colour per slot, always assigned in order (never by value) so the same
# position keeps the same colour across scenes: (fill, border, accent text)
PALETTE = [
    ("#E8F1FF", "#3B82F6", "#1D4ED8"),
    ("#E6F8EC", "#22C55E", "#15803D"),
    ("#FFF1E5", "#F97316", "#C2410C"),
    ("#F4EAFE", "#A855F7", "#7E22CE"),
    ("#FDE8F3", "#EC4899", "#BE185D"),
    ("#E3F7F5", "#14B8A6", "#0F766E"),
]

THEMES = ("kids", "clean")
AMBIENT_FPS = 10  # background motion (clouds, sun) in the kids theme

ARROW_SVG = (
    '<svg viewBox="0 0 64 28" width="64" height="28"><path d="M3 14 H52 M41 4 L54 14 L41 24" '
    'fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1920px;height:1080px;overflow:hidden}
body{font-family:"Noto Sans Arabic","Segoe UI","Noto Sans","Helvetica Neue",Arial,"Noto Color Emoji","Segoe UI Emoji",sans-serif;
  background:linear-gradient(160deg,#FFFBF0 0%,#EEF6FF 100%);color:#1E293B}
.stage{position:relative;width:1920px;height:1080px;padding:54px 90px 0;display:flex;flex-direction:column}
.stage.rtl{direction:rtl}
.bar{width:120px;height:10px;border-radius:6px;background:#3B82F6;margin-bottom:18px}
h1{font-size:74px;line-height:1.15;font-weight:800;color:#0F172A}
.sub{font-size:38px;line-height:1.3;color:#475569;margin-top:10px;font-weight:500}
main{flex:1;display:flex;align-items:center;justify-content:center;padding:10px 0 170px;position:relative}
[data-at]{opacity:0;will-change:opacity,transform}
.card{background:var(--fill);border:5px solid var(--border);border-radius:34px;padding:26px 24px;text-align:center;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;min-width:0}
.card .ico{font-size:var(--ico,104px);line-height:1}
.card .lab{font-size:var(--lab,42px);font-weight:800;line-height:1.2;color:#0F172A;overflow-wrap:anywhere}
.card .cap{font-size:var(--cap,28px);line-height:1.25;color:var(--accent);font-weight:600}
.arrow{color:#94A3B8;display:flex;align-items:center;justify-content:center;flex:none}
.rtl .arrow svg{transform:scaleX(-1)}
.row{display:flex;align-items:stretch;justify-content:center;gap:18px;width:100%}
.row .card{flex:1 1 0;max-width:340px;min-height:300px}
.row .arrow{align-self:center}
/* title */
.title{display:flex;flex-direction:column;align-items:center;text-align:center;gap:26px}
.title .hero{font-size:230px;line-height:1}
.title h1{font-size:120px}
.title .sub{font-size:52px}
/* in -> process -> out */
.ipo{display:flex;align-items:center;justify-content:center;gap:34px;width:100%}
.ipo .col{display:flex;flex-direction:column;gap:22px;flex:1 1 0;max-width:470px}
.ipo .col .card{flex-direction:row;gap:22px;padding:18px 22px;text-align:start;justify-content:flex-start}
.ipo .col .card .ico{font-size:80px}
.ipo .col .card .lab{font-size:38px}
.ipo .col .card .txt{display:flex;flex-direction:column;gap:4px}
/* compare */
.cmp{display:flex;gap:40px;width:100%}
.cmp .side{flex:1;display:flex;flex-direction:column;gap:18px}
.cmp .side h2{font-size:48px;font-weight:800;text-align:center;padding:14px;border-radius:22px;color:#0F172A;background:var(--fill);border:5px solid var(--border)}
.cmp .side .card{flex-direction:row;justify-content:flex-start;text-align:start;padding:16px 22px;gap:20px;border-radius:26px}
.cmp .side .card .ico{font-size:64px}.cmp .side .card .lab{font-size:36px}
/* steps */
.steps{display:flex;flex-direction:column;gap:20px;width:100%;max-width:1400px}
.steps .card{flex-direction:row;justify-content:flex-start;text-align:start;gap:28px;padding:18px 30px;border-radius:30px}
.steps .num{flex:none;width:76px;height:76px;border-radius:50%;background:var(--border);color:#fff;font-size:44px;font-weight:800;display:flex;align-items:center;justify-content:center}
.steps .card .ico{font-size:70px}.steps .card .lab{font-size:42px}
/* equation */
.eq{display:flex;align-items:center;justify-content:center;gap:22px;flex-wrap:nowrap;width:100%}
.eq .card{min-width:200px;padding:26px 28px;--ico:90px;--lab:46px}
.eq .op{font-size:76px;font-weight:800;color:#64748B}
.eq .big{display:flex;flex-direction:column;align-items:center;gap:6px;color:#64748B}
.eq .big span{font-size:34px;font-weight:700;color:#475569;max-width:260px;text-align:center}
.eq .big svg{width:150px;height:60px}
/* quiz */
.quiz{display:flex;flex-direction:column;gap:20px;width:100%;max-width:1300px}
.quiz .opt{flex-direction:row;justify-content:flex-start;text-align:start;gap:28px;padding:20px 32px;border-radius:30px}
.quiz .opt .num{flex:none;width:76px;height:76px;border-radius:50%;background:#fff;border:5px solid var(--border);font-size:42px;font-weight:800;display:flex;align-items:center;justify-content:center;color:#0F172A}
.quiz .opt .lab{font-size:46px}
.quiz .opt.correct{background:#DCFCE7 !important;border-color:#16A34A !important}
.quiz .opt.correct .num{background:#16A34A;border-color:#16A34A;color:#fff}
/* captions */
.captions{position:absolute;left:0;right:0;bottom:46px;display:flex;justify-content:center;padding:0 110px}
.captions span{background:rgba(15,23,42,.88);color:#fff;font-size:44px;line-height:1.35;font-weight:600;padding:16px 34px;border-radius:24px;text-align:center;max-width:1600px}
.captions span:empty{display:none}

.fx{position:absolute;left:0;top:0;z-index:6;pointer-events:none;line-height:1;display:none;will-change:transform}
header{position:relative;z-index:2}
.ipo .mid .card{width:470px;height:470px;min-height:0;border-radius:50%;padding:40px 56px;--ico:120px;--lab:40px}
#wins{display:none}
"""


_KIDS_CSS = """
body.kids{font-family:"Fredoka","Baloo Bhaijaan 2","Noto Sans Arabic","Segoe UI","Noto Sans",Arial,"Noto Color Emoji","Segoe UI Emoji",sans-serif;
  background:linear-gradient(180deg,#7CCBFF 0%,#BFEAFF 45%,#FFF3C4 100%)}
.kids #bg{position:absolute;left:0;top:0;width:1920px;height:1080px;z-index:0;overflow:hidden}
.kids #bg .cloud{position:absolute;left:0;top:0;will-change:transform}
.kids #bg .twinkle{position:absolute;left:0;top:0;font-size:38px;line-height:1}
.kids h1{color:#0B2545;text-shadow:0 4px 0 rgba(255,255,255,.65);letter-spacing:.5px}
.kids .sub{color:#1E3A5F;font-weight:600}
.kids .bar{width:150px;background:linear-gradient(90deg,#FF6B6B,#FFB43B,#4ADE80,#38BDF8,#A78BFA);height:12px}
.kids .card{border-width:6px;box-shadow:0 8px 0 rgba(15,23,42,.13),0 16px 26px rgba(15,23,42,.10)}
.kids .card .lab{font-weight:700}
.kids .cmp .side h2{box-shadow:0 8px 0 rgba(15,23,42,.13);font-weight:700}
.kids .captions span{background:rgba(11,37,69,.92);font-weight:600;border:4px solid rgba(255,255,255,.85);box-shadow:0 8px 20px rgba(11,37,69,.25)}
.kids .eq .op{color:#0B2545}
"""

_JS_TEMPLATE = """
const clamp=(x,a,b)=>Math.min(b,Math.max(a,x));
const ease=p=>1-Math.pow(1-p,3);
const easeIO=u=>u<.5?2*u*u:1-Math.pow(-2*u+2,2)/2;
const backOut=x=>{const c1=1.70158,c3=c1+1;return 1+c3*Math.pow(x-1,3)+c1*Math.pow(x-1,2);};
const ANIM=__ANIM__;
const KIDS=window.THEME==='kids';
let FXLIST=[];
function centerOf(sel){const r=document.querySelector(sel).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2,r.width,r.height];}
function initFx(){
  const stage=document.querySelector('.stage');
  FXLIST=window.FX||[];
  for(const f of FXLIST){
    const ref=f.el||f.src;
    f._el=document.querySelector(ref); f._c=centerOf(ref); if(f.dst) f._d=centerOf(f.dst);
    f._els=[];
    const n=f.type==='fly'?1:((f.type==='burst'||f.type==='orbit')?f.n:0);
    for(let k=0;k<n;k++){
      const d=document.createElement('div'); d.className='fx';
      d.textContent=f.emojis?f.emojis[k%f.emojis.length]:f.emoji;
      d.style.fontSize=(f.size||56)+'px'; stage.appendChild(d); f._els.push(d);
    }
  }
  window.__fxInit=true;
}
function place(d,x,y,s,rot,op){
  d.style.display='block'; d.style.opacity=op;
  d.style.transform='translate('+x+'px,'+y+'px) translate(-50%,-50%) scale('+s+') rotate('+rot+'deg)';
}
function background(t){
  if(!KIDS) return;
  const rays=document.getElementById('rays'); if(rays) rays.setAttribute('transform','rotate('+(t*14)+' 105 105)');
  document.querySelectorAll('#bg .cloud').forEach(c=>{
    const x0=parseFloat(c.dataset.x), sp=parseFloat(c.dataset.sp), y=parseFloat(c.dataset.y);
    const x=((x0+sp*t)%%(2300))-340;
    c.style.transform='translate('+x+'px,'+(y+6*Math.sin(t*0.6+x0))+'px)';
  });
  document.querySelectorAll('#bg .twinkle').forEach(c=>{
    const ph=parseFloat(c.dataset.ph);
    const s=0.55+0.45*Math.abs(Math.sin(t*1.6+ph));
    c.style.transform='translate('+c.dataset.x+'px,'+c.dataset.y+'px) scale('+s+')'; c.style.opacity=0.35+0.65*s;
  });
}
window.renderAt=function(t){
  if(!window.__fxInit) initFx();
  background(t);
  const timed=document.querySelectorAll('[data-at]');
  timed.forEach(el=>{el._es=1;el.style.filter='';});
  for(const f of FXLIST){
    if(f.type==='bump'){
      const u=(t-f.start)/f.dur; if(u>0&&u<1){ f._el._es*=1+f.amp*Math.sin(Math.PI*u); }
    } else if(f.type==='wobble'){
      if(t>=f.start&&t<=f.end){
        const env=Math.min(1,(t-f.start)/.3,(f.end-t)/.3), w=Math.sin(2*Math.PI*f.hz*(t-f.start));
        f._el._es*=1+f.amp*w*env;
        f._el.style.filter='drop-shadow(0 0 '+(14+22*Math.abs(w))*env+'px rgba(255,205,40,.95))';
      }
    }
  }
  timed.forEach(el=>{
    const raw=clamp((t-parseFloat(el.dataset.at))/ANIM,0,1), es=el._es||1;
    if(el instanceof SVGElement){ el.style.opacity=ease(raw); return; }
    if(KIDS){
      const e=backOut(raw);
      el.style.opacity=Math.min(1,raw*3);
      el.style.transform='translateY('+((1-raw)*34)+'px) scale('+((0.55+0.45*e)*es)+') rotate('+((1-raw)*-3)+'deg)';
    } else {
      const p=ease(raw);
      el.style.opacity=p;
      el.style.transform='translateY('+((1-p)*26)+'px) scale('+((0.94+0.06*p)*es)+')';
    }
  });
  for(const f of FXLIST){
    if(f.type==='fly'){
      const d=f._els[0], u=(t-f.start)/f.dur;
      if(u<=0||u>=1){d.style.display='none';continue;}
      const e=easeIO(u), x0=f._c[0],y0=f._c[1],x1=f._d[0],y1=f._d[1];
      const cx=(x0+x1)/2, cy=Math.min(y0,y1)+f.arc;
      const x=(1-e)*(1-e)*x0+2*(1-e)*e*cx+e*e*x1, y=(1-e)*(1-e)*y0+2*(1-e)*e*cy+e*e*y1;
      place(d,x,y,1-(1-f.shrink)*e,e*360*(f.spin||0),u>.88?(1-u)/.12:1);
    } else if(f.type==='orbit'){
      const on=t>=f.start&&t<=f.end, fade=Math.min(1,(t-f.start)/.35,(f.end-t)/.35);
      const r=Math.max(f._c[2],f._c[3])/2+f.pad;
      f._els.forEach((d,k)=>{
        if(!on){d.style.display='none';return;}
        const th=1.7*(t-f.start)+k*2*Math.PI/f._els.length;
        place(d,f._c[0]+r*Math.cos(th),f._c[1]+r*0.92*Math.sin(th),0.8+0.3*Math.sin(3*t+k),0,Math.max(0,fade));
      });
    } else if(f.type==='burst'){
      const u=(t-f.at)/1.0;
      f._els.forEach((d,k)=>{
        if(u<=0||u>=1){d.style.display='none';return;}
        const th=k*2*Math.PI/f._els.length+0.4, dist=f.dist*(1-Math.pow(1-u,3));
        place(d,f._c[0]+dist*Math.cos(th),f._c[1]+dist*Math.sin(th),0.5+0.9*(1-u),u*200*(k%%2?1:-1),1-u*u);
      });
    }
  }
  const caps=window.CAPTIONS||[];
  let text='';
  for(const c of caps){ if(t>=c.start-1e-6 && t<c.end){ text=c.text; } }
  document.getElementById('cap').textContent=text;
  const ans=window.ANSWER_AT;
  if(ans!==undefined){ document.querySelectorAll('.opt').forEach(o=>{
    const hit=o.dataset.correct==='1';
    o.classList.toggle('correct', hit && t>=ans);
    if(t>=ans && !hit) o.style.opacity=0.45;
  }); }
};
""".replace("__ANIM__", str(ANIM_SECONDS)).replace("%%", "%")


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


def _style(slot: int) -> str:
    fill, border, accent = PALETTE[slot % len(PALETTE)]
    return f"--fill:{fill};--border:{border};--accent:{accent}"


def _js_json(obj) -> str:
    # valid JS literal; '</' is escaped so text can never close the <script> tag
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


class _Fx:
    """Collects the moving effects of one scene (tokens flying between items,
    sparkle bursts, pulses). They are plain data; the page script draws them as
    a pure function of time, so rendering stays deterministic."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.windows: list[tuple[float, float, int]] = []

    def _win(self, a: float, b: float, fps: int = FPS) -> None:
        # long effects only need a calmer frame rate
        self.windows.append((round(max(a, 0.0), 3), round(b, 3), fps if b - a <= 5 else 12))

    def fly(self, src: str, dst: str, start: float, dur: float, emoji: str,
            size: int = 60, shrink: float = 0.55, arc: float = -110, spin: float = 0.0) -> None:
        start = max(start, 0.0)
        self.items.append(dict(type="fly", src=src, dst=dst, start=round(start, 3), dur=round(dur, 3),
                               emoji=emoji or "✨", size=size, shrink=shrink, arc=arc, spin=spin))
        self._win(start, start + dur)

    def bump(self, el: str, start: float, dur: float = 0.45, amp: float = 0.09) -> None:
        start = max(start, 0.0)
        self.items.append(dict(type="bump", el=el, start=round(start, 3), dur=dur, amp=amp))
        self._win(start, start + dur)

    def wobble(self, el: str, start: float, end: float, amp: float = 0.035, hz: float = 2.2) -> None:
        if end - start < 0.3:
            return
        self.items.append(dict(type="wobble", el=el, start=round(start, 3), end=round(end, 3), amp=amp, hz=hz))
        self._win(start, end)

    def orbit(self, el: str, start: float, end: float, emojis: list[str], n: int = 6,
              pad: float = 34, size: int = 40) -> None:
        if end - start < 0.3:
            return
        self.items.append(dict(type="orbit", el=el, start=round(start, 3), end=round(end, 3),
                               emojis=emojis, n=n, pad=pad, size=size))
        self._win(start, end)

    def burst(self, el: str, at: float, emojis: list[str], n: int = 10, dist: float = 230, size: int = 46) -> None:
        at = max(at, 0.0)
        self.items.append(dict(type="burst", el=el, at=round(at, 3), emojis=emojis, n=n, dist=dist, size=size))
        self._win(at, at + 1.0)


def _card(item: Item, slot: int, at: float, extra_style: str = "", cls: str = "card", eid: str = "") -> str:
    ico = f'<div class="ico">{_e(item.icon)}</div>' if item.icon else ""
    cap = f'<div class="cap">{_e(item.caption)}</div>' if item.caption else ""
    idattr = f' id="{eid}"' if eid else ""
    return (
        f'<div{idattr} class="{cls}" data-at="{at:.3f}" style="{_style(slot)};{extra_style}">'
        f'{ico}<div class="lab">{_e(item.label)}</div>{cap}</div>'
    )


def _arrow(at: float) -> str:
    return f'<div class="arrow" data-at="{at:.3f}">{ARROW_SVG}</div>'


def _sizes(n: int) -> str:
    if n <= 3:
        return "--ico:120px;--lab:46px;--cap:30px"
    if n == 4:
        return "--ico:100px;--lab:40px;--cap:27px"
    return "--ico:84px;--lab:34px;--cap:24px"


# ---------------------------------------------------------------- layouts ----


def _layout_title(v: Visual, tl: Timeline, fx: _Fx) -> str:
    hero = v.items[0].icon if v.items and v.items[0].icon else "🎓"
    sub = f'<div class="sub" data-at="{LEAD_IN_AT + 0.35:.3f}">{_e(v.subheading)}</div>' if v.subheading else ""
    fx.orbit("#hero", 0.5, max(tl.duration - 0.4, 1.0), ["✨", "⭐", "✨", "💫", "✨"], n=5, pad=60, size=44)
    return (
        f'<div class="title"><div id="hero" class="hero" data-at="0.05">{_e(hero)}</div>'
        f'<h1 data-at="{LEAD_IN_AT:.3f}">{_e(v.heading)}</h1>{sub}</div>'
    )


LEAD_IN_AT = 0.25


def _layout_flow(v: Visual, tl: Timeline, fx: _Fx) -> str:
    n = len(v.items)
    parts = []
    prev_icon = ""
    for i, item in enumerate(v.items):
        at = tl.item_time(item.cue, i, n)
        if i > 0:
            parts.append(_arrow(at - 0.1))
            fx.fly(f"#fl{i - 1}", f"#fl{i}", at - 0.6, 0.6, prev_icon or "✨", size=54, shrink=1.0, arc=-70)
        parts.append(_card(item, i, at, _sizes(n), eid=f"fl{i}"))
        prev_icon = item.icon
    return f'<div class="row">{"".join(parts)}</div>'


def _layout_cycle(v: Visual, tl: Timeline, fx: _Fx) -> str:
    n = len(v.items)
    vw, vh = 1500, 640
    cx, cy, rx, ry = vw / 2, vh / 2, 520, 215
    pts = []
    for i in range(n):
        a = -math.pi / 2 + 2 * math.pi * i / n
        pts.append((cx + rx * math.cos(a), cy + ry * math.sin(a), a))
    cards, arrows = [], []
    for i, item in enumerate(v.items):
        x, y, _ = pts[i]
        at = tl.item_time(item.cue, i, n)
        style = f"position:absolute;left:{x / vw * 100:.2f}%;top:{y / vh * 100:.2f}%;width:300px;margin:-95px 0 0 -150px;--ico:70px;--lab:34px;--cap:23px;{_style(i)}"
        ico = f'<div class="ico" style="font-size:70px">{_e(item.icon)}</div>' if item.icon else ""
        cap = f'<div class="cap">{_e(item.caption)}</div>' if item.caption else ""
        cards.append(
            f'<div id="cy{i}" class="card" data-at="{at:.3f}" style="{style}">{ico}<div class="lab">{_e(item.label)}</div>{cap}</div>'
        )
        if i > 0:
            fx.fly(f"#cy{i - 1}", f"#cy{i}", at - 0.65, 0.6, v.items[i - 1].icon or "💧", size=54, shrink=1.0, arc=-60)
        # arrow along the ellipse from item i to item i+1 (clockwise)
        a0, a1 = pts[i][2], pts[i][2] + 2 * math.pi / n
        j = (i + 1) % n

        def _inside(px: float, py: float, k: int) -> bool:
            return abs(px - pts[k][0]) < 150 + 22 and abs(py - pts[k][1]) < 112 + 22

        steps = 400
        s_ang, e_ang = a0, a1
        for t in range(steps):  # walk out of the current card
            s_ang = a0 + (a1 - a0) * t / steps
            if not _inside(cx + rx * math.cos(s_ang), cy + ry * math.sin(s_ang), i):
                break
        for t in range(steps):  # walk back out of the next card
            e_ang = a1 - (a1 - a0) * t / steps
            if not _inside(cx + rx * math.cos(e_ang), cy + ry * math.sin(e_ang), j):
                break
        sx, sy = cx + rx * math.cos(s_ang), cy + ry * math.sin(s_ang)
        ex, ey = cx + rx * math.cos(e_ang), cy + ry * math.sin(e_ang)
        nxt_at = tl.item_time(v.items[(i + 1) % n].cue, (i + 1) % n, n) if i + 1 < n else at + 1.0
        arrows.append(
            f'<path d="M{sx:.1f} {sy:.1f} A{rx} {ry} 0 0 1 {ex:.1f} {ey:.1f}" fill="none" stroke="#94A3B8" '
            f'stroke-width="7" stroke-linecap="round" marker-end="url(#ah)" data-at="{nxt_at - 0.1:.3f}"/>'
        )
    svg = (
        f'<svg viewBox="0 0 {vw} {vh}" style="position:absolute;inset:0;width:100%;height:100%;overflow:visible">'
        '<defs><marker id="ah" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="4.2" markerHeight="4.2" orient="auto">'
        '<path d="M0 0 L10 5 L0 10 z" fill="#94A3B8"/></marker></defs>' + "".join(arrows) + "</svg>"
    )
    return f'<div style="position:relative;width:{vw}px;height:{vh}px">{svg}{"".join(cards)}</div>'


def _layout_ipo(v: Visual, tl: Timeline, fx: _Fx) -> str:
    """Inputs fly into the 'factory', it works (pulse + sparkles), then a burst
    and the outputs fly out and pop onto the screen."""
    n_in, n_out = len(v.inputs), len(v.outputs)
    total = n_in + 1 + n_out
    proc = v.process
    FLY_DELAY, FLY_DUR, OUT_FLY = 0.35, 0.75, 0.55

    in_t = [tl.item_time(i.cue, k, n_in) for k, i in enumerate(v.inputs)]
    first_in = min(in_t) if in_t else 0.5
    arrivals = [t + FLY_DELAY + FLY_DUR for t in in_t]
    first_arrive, last_arrive = min(arrivals), max(arrivals)

    proc_cue = tl.cue_time(proc.cue) if proc and proc.cue else None
    proc_at = first_in + 0.15 if proc_cue is None else min(proc_cue, first_in + 0.15)

    base_out = last_arrive + 0.65
    out_t = [
        max(tl.item_time(o.cue, n_in + 1 + k, total), base_out + 0.45 * k) for k, o in enumerate(v.outputs)
    ]
    first_launch = min(out_t) - OUT_FLY if out_t else last_arrive + 0.3

    # ---- effects
    for k, item in enumerate(v.inputs):
        fx.fly(f"#in{k}", "#proc", in_t[k] + FLY_DELAY, FLY_DUR, item.icon or "✨", size=64, shrink=0.5)
        fx.bump("#proc", arrivals[k] - 0.2, 0.45, 0.07)
    fx.wobble("#proc", first_arrive, first_launch, amp=0.03)
    fx.orbit("#proc", first_arrive, first_launch, ["✨", "⭐", "✨", "💫", "✨", "⭐"], n=6, pad=30, size=40)
    fx.burst("#proc", first_launch, ["✨", "⭐", "💥", "✨", "⭐"], n=10, dist=250, size=46)
    for k, item in enumerate(v.outputs):
        fx.fly("#proc", f"#out{k}", out_t[k] - OUT_FLY, OUT_FLY, item.icon or "✨", size=64, shrink=1.0, arc=-80)

    def column(items: list[Item], base_slot: int, prefix: str, times: list[float]) -> str:
        out = []
        for i, item in enumerate(items):
            ico = f'<div class="ico">{_e(item.icon)}</div>' if item.icon else ""
            cap = f'<div class="cap">{_e(item.caption)}</div>' if item.caption else ""
            out.append(
                f'<div id="{prefix}{i}" class="card" data-at="{times[i]:.3f}" style="{_style(base_slot + i)}">{ico}'
                f'<div class="txt"><div class="lab">{_e(item.label)}</div>{cap}</div></div>'
            )
        return f'<div class="col">{"".join(out)}</div>'

    mid = _card(proc, 3, proc_at, "", "card", eid="proc") if proc else ""
    return (
        '<div class="ipo">'
        + column(v.inputs, 0, "in", in_t)
        + _arrow(first_in + 0.1)
        + f'<div class="mid">{mid}</div>'
        + _arrow(first_launch)
        + column(v.outputs, 1, "out", out_t)
        + "</div>"
    )


def _layout_compare(v: Visual, tl: Timeline, fx: _Fx) -> str:
    def side(title: str, items: list[Item], slot: int, offset: int, total: int) -> str:
        t_at = LEAD_IN_AT + (0.0 if slot == 0 else 0.6)
        head = f'<h2 data-at="{t_at:.3f}" style="{_style(slot)}">{_e(title)}</h2>'
        cards = "".join(
            _card(it, slot, tl.item_time(it.cue, offset + k, total), "", "card") for k, it in enumerate(items)
        )
        return f'<div class="side">{head}{cards}</div>'

    total = len(v.left) + len(v.right)
    return (
        '<div class="cmp">'
        + side(v.left_title, v.left, 0, 0, total)
        + side(v.right_title, v.right, 2, len(v.left), total)
        + "</div>"
    )


def _layout_steps(v: Visual, tl: Timeline, fx: _Fx) -> str:
    n = len(v.items)
    rows = []
    for i, item in enumerate(v.items):
        at = tl.item_time(item.cue, i, n)
        ico = f'<div class="ico">{_e(item.icon)}</div>' if item.icon else ""
        cap = f'<div class="cap">{_e(item.caption)}</div>' if item.caption else ""
        rows.append(
            f'<div class="card" data-at="{at:.3f}" style="{_style(i)}"><div class="num">{i + 1}</div>{ico}'
            f'<div style="display:flex;flex-direction:column;gap:4px"><div class="lab">{_e(item.label)}</div>{cap}</div></div>'
        )
    return f'<div class="steps">{"".join(rows)}</div>'


def _layout_equation(v: Visual, tl: Timeline, fx: _Fx) -> str:
    total = len(v.reactants) + len(v.products)
    parts: list[str] = []
    k = 0

    def part_card(p, slot, idx):
        at = tl.item_time(p.cue, idx, total)
        ico = f'<div class="ico">{_e(p.icon)}</div>' if p.icon else ""
        return at, (
            f'<div class="card" data-at="{at:.3f}" style="{_style(slot)}">{ico}<div class="lab">{_e(p.text)}</div></div>'
        )

    first_prod_at = None
    for i, p in enumerate(v.reactants):
        at, html_ = part_card(p, 0, k)
        if i > 0:
            parts.append(f'<div class="op" data-at="{at - 0.1:.3f}">+</div>')
        parts.append(html_)
        k += 1
    arrow_at = tl.cue_time(v.over_arrow) if v.over_arrow else None
    prod_cards = []
    for i, p in enumerate(v.products):
        at, html_ = part_card(p, 1, k)
        first_prod_at = at if first_prod_at is None else min(first_prod_at, at)
        if i > 0:
            prod_cards.append(f'<div class="op" data-at="{at - 0.1:.3f}">+</div>')
        prod_cards.append(html_)
        k += 1
    arrow_at = arrow_at if arrow_at is not None else (first_prod_at or 1.0) - 0.4
    over = f"<span>{_e(v.over_arrow)}</span>" if v.over_arrow else ""
    parts.append(f'<div id="eqarrow" class="big" data-at="{arrow_at:.3f}">{over}{ARROW_SVG}</div>')
    parts.extend(prod_cards)
    boom = (first_prod_at or arrow_at + 1.0) - 0.15
    fx.orbit("#eqarrow", arrow_at + 0.2, boom, ["✨", "⭐", "✨", "💫"], n=4, pad=30, size=38)
    fx.wobble("#eqarrow", arrow_at + 0.2, boom, amp=0.05, hz=2.0)
    fx.burst("#eqarrow", boom, ["✨", "⭐", "💥", "✨", "⭐"], n=10, dist=240, size=46)
    return f'<div class="eq">{"".join(parts)}</div>'


def _layout_quiz(v: Visual, tl: Timeline, fx: _Fx) -> tuple[str, float]:
    n = len(v.options)
    letters = "ABCD"
    rows = []
    for i, opt in enumerate(v.options):
        # options appear while the question is being read
        at = tl.even_time(i, n, span=0.55)
        correct = "1" if i == v.answer_index else "0"
        rows.append(
            f'<div id="opt{i}" class="card opt" data-correct="{correct}" data-at="{at:.3f}" style="{_style(i)}">'
            f'<div class="num">{letters[i]}</div><div class="lab">{_e(opt)}</div></div>'
        )
    ans = tl.cue_time(v.options[v.answer_index], last=True)
    answer_at = ans if ans is not None else LEAD_IN_AT + 0.82 * tl.speech
    answer_at = max(answer_at, LEAD_IN_AT + 0.5 * tl.speech)
    fx.bump(f"#opt{v.answer_index}", answer_at, 0.5, 0.05)
    fx.burst(f"#opt{v.answer_index}", answer_at, ["🎉", "⭐", "✨", "🎊", "⭐"], n=12, dist=280, size=50)
    return f'<div class="quiz">{"".join(rows)}</div>', answer_at


# ------------------------------------------------------------- kids theme ----


@functools.lru_cache(maxsize=1)
def _font_css() -> str:
    from app.services.edu import fonts_b64 as f

    def face(family: str, weight: str, b64: str) -> str:
        return (
            f'@font-face{{font-family:"{family}";font-weight:{weight};font-style:normal;'
            f'src:url(data:font/woff2;base64,{b64}) format("woff2")}}'
        )

    return (
        face("Fredoka", "600", f.FREDOKA_600)
        + face("Fredoka", "700 900", f.FREDOKA_700)
        + face("Baloo Bhaijaan 2", "100 900", f.BALOO_AR_700)
    )


def _cloud(x: int, y: int, speed: float, scale: float) -> str:
    return (
        f'<svg class="cloud" data-x="{x}" data-y="{y}" data-sp="{speed}" width="{int(300 * scale)}" '
        f'height="{int(150 * scale)}" viewBox="0 0 300 150"><g fill="#fff" opacity=".92">'
        '<circle cx="90" cy="85" r="50"/><circle cx="150" cy="60" r="58"/><circle cx="215" cy="85" r="48"/>'
        '<rect x="60" y="85" width="190" height="50" rx="25"/></g></svg>'
    )


def _kids_background(language: str) -> str:
    sun_left = 40 if language == "ar" else 1650
    rays = "".join(
        f'<rect x="97" y="2" width="16" height="34" rx="8" fill="#FFC94A" transform="rotate({a} 105 105)"/>'
        for a in range(0, 360, 30)
    )
    sun = (
        f'<svg style="position:absolute;left:{sun_left}px;top:26px" width="230" height="230" viewBox="0 0 210 210">'
        f'<g id="rays">{rays}</g><circle cx="105" cy="105" r="62" fill="#FFD43B"/>'
        '<circle cx="85" cy="97" r="7" fill="#7A4B00"/><circle cx="125" cy="97" r="7" fill="#7A4B00"/>'
        '<path d="M80 120 Q105 146 130 120" fill="none" stroke="#7A4B00" stroke-width="6" stroke-linecap="round"/>'
        '<circle cx="72" cy="116" r="9" fill="#FF9A8B" opacity=".55"/><circle cx="138" cy="116" r="9" fill="#FF9A8B" opacity=".55"/></svg>'
    )
    clouds = _cloud(200, 250, 16, 1.0) + _cloud(1150, 130, 11, 0.8) + _cloud(1750, 470, 20, 0.7) + _cloud(650, 560, 13, 0.9)
    twinkles = "".join(
        f'<div class="twinkle" data-x="{x}" data-y="{y}" data-ph="{ph}">✨</div>'
        for x, y, ph in [(330, 330, 0.2), (1500, 330, 1.4), (1820, 620, 2.2), (60, 700, 3.1), (1380, 80, 4.0)]
    )
    hills = (
        '<svg style="position:absolute;left:0;bottom:0" width="1920" height="230" viewBox="0 0 1920 230" preserveAspectRatio="none">'
        '<path d="M0 120 Q300 20 640 110 T1300 90 T1920 100 V230 H0Z" fill="#9BE58F"/>'
        '<path d="M0 170 Q380 90 760 160 T1500 150 T1920 160 V230 H0Z" fill="#6FCF7A"/></svg>'
    )
    return f'<div id="bg">{sun}{clouds}{twinkles}{hills}</div>'


# ------------------------------------------------------------------- page ----


def build_page(scene: Scene, tl: Timeline, language: str, theme: str = "kids") -> tuple[str, list[float], list[float]]:
    """Return (html, animated_times, instant_times) for the scene. The dense-frame
    windows of the moving effects are embedded in the page (see `page_windows`)."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}; choose from {THEMES}")
    v = scene.visual
    fx = _Fx()
    answer_at: Optional[float] = None
    if v.layout == "quiz":
        body, answer_at = _layout_quiz(v, tl, fx)
    else:
        body = {
            "title": _layout_title,
            "flow": _layout_flow,
            "cycle": _layout_cycle,
            "in_process_out": _layout_ipo,
            "compare": _layout_compare,
            "steps": _layout_steps,
            "equation": _layout_equation,
        }[v.layout](v, tl, fx)

    header = ""
    if v.layout != "title":
        sub = f'<div class="sub" data-at="0.35">{_e(v.subheading)}</div>' if v.subheading else ""
        header = f'<header><div class="bar"></div><h1 data-at="0.15">{_e(v.heading)}</h1>{sub}</header>'

    caps = tl.captions()
    caps_js = "[" + ",".join(
        '{"start":%.3f,"end":%.3f,"text":%s}' % (c.start, c.end, _js_str(c.text)) for c in caps
    ) + "]"
    answer_js = f"window.ANSWER_AT={answer_at:.3f};" if answer_at is not None else ""
    direction = "rtl" if language == "ar" else "ltr"
    kids = theme == "kids"
    extra_css = (_font_css() + _KIDS_CSS) if kids else ""
    bg = _kids_background(language) if kids else ""
    wins = ";".join(f"{a}:{b}:{fps}" for a, b, fps in fx.windows)
    page = (
        f'<!doctype html><html lang="{language}"><head><meta charset="utf-8"><style>{_CSS}{extra_css}</style></head>'
        f'<body class="{theme}"><div class="stage {direction}">{bg}{header}<main>{body}</main>'
        f'<div class="captions"><span id="cap"></span></div></div><div id="wins" data-w="{wins}"></div>'
        f"<script>window.THEME={_js_json(theme)};window.FX={_js_json(fx.items)};"
        f"window.CAPTIONS={caps_js};{answer_js}{_JS_TEMPLATE}</script></body></html>"
    )

    animated = sorted({float(t) for t in re.findall(r'data-at="(-?[\d.]+)"', body + header)})
    instant = [c.start for c in caps]
    if answer_at is not None:
        animated.append(answer_at)
    return page, animated, instant


def page_windows(page_html: str) -> list[tuple[float, float, int]]:
    """Dense-frame windows (start, end, fps) of the moving effects in a page."""
    m = re.search(r'id="wins" data-w="([^"]*)"', page_html)
    out: list[tuple[float, float, int]] = []
    if m and m.group(1):
        for chunk in m.group(1).split(";"):
            a, b, fps = chunk.split(":")
            out.append((float(a), float(b), int(fps)))
    return out


def _js_str(text: str) -> str:
    # json.dumps output is a valid JS string literal; '</' is escaped so the
    # text can never close the <script> tag.
    return json.dumps(text, ensure_ascii=False).replace("</", "<\\/")


def keyframe_times(
    animated: list[float],
    instant: list[float],
    duration: float,
    windows: tuple | list = (),
    ambient_fps: int = 0,
) -> list[float]:
    """Times at which a screenshot is needed. Dense while something animates,
    a single frame for a static change, plus a slow ambient grid when the
    background moves."""
    times = {0.0}
    steps = int(math.ceil(ANIM_SECONDS * FPS))
    for t in animated:
        for k in range(steps + 1):
            times.add(round(max(0.0, t) + k / FPS, 3))
    for t in instant:
        times.add(round(t, 3))
    for a, b, fps in windows:
        k = 0
        while a + k / fps <= b + 1e-9:
            times.add(round(a + k / fps, 3))
            k += 1
    if ambient_fps:
        k = 0
        while k / ambient_fps < duration:
            times.add(round(k / ambient_fps, 3))
            k += 1
    # never closer than one video frame: every kept screenshot lasts >= 1/FPS
    kept: list[float] = []
    for t in sorted(x for x in times if x < duration - 1e-3):
        if not kept or t - kept[-1] >= 1 / FPS - 1e-3:
            kept.append(t)
    return kept


# ------------------------------------------------------------------ video ----


@dataclass
class SceneClip:
    path: str
    duration: float
    frames: int


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:600]}")


def render_scene(
    scene: Scene,
    audio_path: str,
    speech_seconds: float,
    out_path: str,
    language: str = "en",
    work_dir: Optional[str] = None,
    theme: str = "kids",
) -> SceneClip:
    """Render `scene` to `out_path` (mp4, 1920x1080, with `audio_path` muxed in)."""
    from playwright.sync_api import sync_playwright

    tl = Timeline(scene.narration, speech_seconds)
    page_html, animated, instant = build_page(scene, tl, language, theme)
    times = keyframe_times(
        animated, instant, tl.duration,
        windows=page_windows(page_html),
        ambient_fps=AMBIENT_FPS if theme == "kids" else 0,
    )

    own_tmp = work_dir is None
    work = os.path.abspath(work_dir or tempfile.mkdtemp(prefix="edu_scene_"))
    os.makedirs(work, exist_ok=True)
    try:
        frame_files: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--font-render-hinting=none"])
            try:
                ctx = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
                ctx.route("**/*", lambda route: route.abort() if not route.request.url.startswith(("data:", "about:")) else route.continue_())
                page = ctx.new_page()
                page.set_content(page_html, wait_until="load")
                if theme == "kids":  # make sure the embedded fonts are decoded before measuring
                    page.evaluate(
                        "Promise.all([document.fonts.load('700 40px Fredoka'),"
                        "document.fonts.load('600 40px Fredoka'),"
                        """document.fonts.load('700 40px "Baloo Bhaijaan 2"', 'ع')])"""
                    )
                page.evaluate("document.fonts.ready")
                for idx, t in enumerate(times):
                    page.evaluate("t => window.renderAt(t)", t)
                    fp = os.path.join(work, f"f{idx:05d}.jpg")
                    page.screenshot(path=fp, type="jpeg", quality=92)
                    frame_files.append(fp)
            finally:
                browser.close()

        # concat list with per-frame durations
        list_path = os.path.join(work, "frames.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            frame_files = [x.replace("\\", "/") for x in frame_files]  # ffmpeg list: forward slashes (Windows)
            for i, fp in enumerate(frame_files):
                end = times[i + 1] if i + 1 < len(times) else tl.duration
                f.write(f"file '{fp}'\nduration {max(end - times[i], 1 / FPS):.4f}\n")
            f.write(f"file '{frame_files[-1]}'\n")  # concat demuxer quirk: repeat last

        _ffmpeg(
            [
                "-f", "concat", "-safe", "0", "-i", list_path,
                "-i", audio_path,
                "-vf", f"fps={FPS},format=yuv420p",
                "-af", f"adelay={int(0.35 * 1000)}:all=1,apad=whole_dur={tl.duration:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k",
                "-t", f"{tl.duration:.3f}",
                "-movflags", "+faststart",
                out_path,
            ]
        )
        return SceneClip(path=out_path, duration=tl.duration, frames=len(frame_files))
    finally:
        if own_tmp:
            shutil.rmtree(work, ignore_errors=True)
