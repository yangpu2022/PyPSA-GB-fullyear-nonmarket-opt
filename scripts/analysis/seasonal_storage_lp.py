"""
Seasonal storage-dispatch LP for PyPSA-GB.

Builds a 1-bus national copperplate version of a solved scenario (generators
aggregated by carrier using *available* VRE profiles, the storage fleet, and the
hydrogen store), then solves the WHOLE YEAR in one optimisation (full foresight).
This is tiny (~10^5 vars) so it does not hit the memory wall of the full-network
single solve, and - unlike the 24h rolling market - it lets the H2 store shift
energy seasonally (fill in windy/summer months, discharge in winter droughts).

On top of the solve it builds a load-duration / residual-load-duration (RLDC)
view across the FES scenarios (2030 vs 2040) that shows the long-duration storage
NEED and how the VRE surplus is utilised by storage, consolidated - per the repo
convention - into ONE Excel workbook + ONE tabbed vanilla-SVG HTML report.

Outputs, per scenario, to resources/analysis/:
  seasonal_storage_<scn>_soc.csv     hourly H2 SoC, electrolysis, turbine, battery SoC, unserved
  seasonal_storage_<scn>_genmix.csv  hourly generation mix + storage charge/discharge + electrolysis
  seasonal_storage_<scn>_sizing.csv  H2 store energy (GWh) -> annual unserved energy (TWh)
  seasonal_storage_<scn>_ldc.csv     duration curves (demand / avail VRE / net pre & post storage), sorted desc
  seasonal_storage_<scn>_surplus.csv one-row surplus/deficit energy decomposition + firm-peak + H2 swing
Consolidated:
  seasonal_storage_analysis.xlsx     multi-sheet workbook (README first)
  seasonal_storage_report.html       tabbed, theme-aware SVG report (supersedes the orphan
                                     full_year_generation_storage.html)

Run:
  python scripts/analysis/seasonal_storage_lp.py             # fast: LDC + report from existing solved outputs (no LP solve)
  python scripts/analysis/seasonal_storage_lp.py --recompute # re-solve the full-year LP + sizing sweep first, then report
"""
import argparse, json, os, warnings, numpy as np, pandas as pd, pypsa
warnings.filterwarnings('ignore')

MK = r'C:\models\pypsa-gb\resources\market'
OUTDIR = r'C:\models\pypsa-gb\resources\analysis'
# Scenario network key -> label components (horizon year, weather year). A 2x2 grid:
# FES 2025 Holistic Transition at 2030 & 2040, each on weather year 2010 (low wind,
# stress case) and 2013 (the model's default renewables year). Both weather years are
# the only ones with solved wholesale networks on disk; more years need the offline
# scripts/gap/build_vre_profiles.py path (41-year cutout set), not just an LP re-solve.
SCEN = {
    'HT30_market_w2010':    {'year': '2030', 'wy': '2010'},
    'HT30_market':          {'year': '2030', 'wy': '2013'},
    'HT40_market_w2010':    {'year': '2040', 'wy': '2010'},
    'HT40_market_fullyear': {'year': '2040', 'wy': '2013'},
}


def _label(scn):
    m = SCEN[scn]
    return f"{m['year']} w{m['wy']}"


VOLL = 6000.0
SOLVER = 'gurobi'

VRE = {'wind_offshore', 'wind_onshore', 'embedded_wind', 'solar_pv', 'embedded_solar',
       'marine', 'tidal_stream', 'shoreline_wave'}

# ── HTML report template (theme-aware, vanilla-SVG; DATA injected by build_report) ──
_HTML_TMPL = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seasonal storage & the residual-load duration curve - PyPSA-GB FES 2030/2040</title>
<style>
  :root{ --bg:#fcfcfb;--panel:#fff;--ink:#1a1d21;--ink2:#5a6068;--muted:#8b929b;--line:#e7e6e2;--grid:#efeee9;
    --accent:#0f6d5f;--demand:#5a6068;--avail:#3b7dd8;--pre:#e0742b;--post:#3f8f5b;
    --served:#3b7dd8;--bat:#e0a72b;--h2:#8a63d2;--curt:#c1432e;--gas:#e0742b;--uns:#b0182b;
    --shadow:0 1px 2px rgba(0,0,0,.05),0 4px 16px rgba(0,0,0,.04);}
  @media (prefers-color-scheme:dark){:root{--bg:#14171a;--panel:#1c2024;--ink:#eceef0;--ink2:#a9b0b8;--muted:#79818b;--line:#2c3237;--grid:#262b30;--accent:#4fd1bd;--demand:#a9b0b8;--avail:#5fa0ef;--pre:#f0954f;--post:#5cb47b;--served:#5fa0ef;--bat:#e6bb55;--h2:#a98ae0;--curt:#e0664f;--gas:#f0954f;--uns:#e0546a;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 22px rgba(0,0,0,.35);}}
  :root[data-theme="dark"]{--bg:#14171a;--panel:#1c2024;--ink:#eceef0;--ink2:#a9b0b8;--muted:#79818b;--line:#2c3237;--grid:#262b30;--accent:#4fd1bd;--demand:#a9b0b8;--avail:#5fa0ef;--pre:#f0954f;--post:#5cb47b;--served:#5fa0ef;--bat:#e6bb55;--h2:#a98ae0;--curt:#e0664f;--gas:#f0954f;--uns:#e0546a;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 22px rgba(0,0,0,.35);}
  :root[data-theme="light"]{--bg:#fcfcfb;--panel:#fff;--ink:#1a1d21;--ink2:#5a6068;--muted:#8b929b;--line:#e7e6e2;--grid:#efeee9;--accent:#0f6d5f;--demand:#5a6068;--avail:#3b7dd8;--pre:#e0742b;--post:#3f8f5b;--served:#3b7dd8;--bat:#e0a72b;--h2:#8a63d2;--curt:#c1432e;--gas:#e0742b;--uns:#b0182b;}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1060px;margin:0 auto;padding:14px 22px 60px}
  h1{font-size:24px;font-weight:680;letter-spacing:-.02em;margin:0 0 6px} h2{font-size:18px;font-weight:640;margin:6px 0 2px}
  h3{font-size:14px;font-weight:620;margin:0 0 2px} .sub{color:var(--ink2);margin:0 0 4px}
  .meta{color:var(--muted);font-size:12.5px;margin:8px 0 0} .lede{color:var(--ink2);margin:2px 0 12px;max-width:70ch}
  .cap{color:var(--muted);font-size:12.5px;margin:2px 0 6px}
  .tabs{display:flex;gap:4px;margin:14px 0 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .tab{border:0;background:none;color:var(--ink2);font:inherit;font-weight:560;padding:10px 15px;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px}
  .tab:hover{color:var(--ink)} .tab[aria-selected="true"]{color:var(--ink);box-shadow:inset 0 -2px 0 var(--accent)}
  .pp{display:none} .pp.on{display:block}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px} @media(max-width:820px){.grid2{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;box-shadow:var(--shadow);margin:0 0 16px}
  .themebtn{position:fixed;top:12px;right:14px;border:1px solid var(--line);background:var(--panel);color:var(--ink2);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit;font-size:12.5px;z-index:30}
  .lg{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin:4px 0 2px}
  .lg span{display:inline-flex;align-items:center;gap:5px} .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
  .tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);font-size:12px;padding:6px 9px;border-radius:7px;opacity:0;transition:opacity .1s;white-space:nowrap;z-index:40;box-shadow:var(--shadow)}
  table{border-collapse:collapse;font-size:12.5px;width:100%} th,td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--line)} th:first-child,td:first-child{text-align:left}
  .kpi{display:flex;gap:18px;flex-wrap:wrap;margin:2px 0 8px} .kpi div{font-size:12.5px;color:var(--ink2)} .kpi b{display:block;font-size:20px;color:var(--ink);font-weight:680}
  svg{width:100%;height:auto;display:block}
</style></head><body>
<button class="themebtn" onclick="toggleTheme()">&#9680; theme</button>
<div class="wrap">
<h1>Seasonal storage and the residual-load duration curve</h1>
<p class="sub">GB, FES 2025 Holistic Transition, 2030 &amp; 2040, on weather years 2010 (low wind) and 2013. Full-year single-node copperplate LP with a seasonal H2 store.</p>
<p class="meta">Duration curves sort each hourly series independently over the year. Negative net load = VRE surplus. National energy balance only (no transmission). Source: seasonal_storage_lp.py.</p>
<nav class="tabs" role="tablist">
  <button class="tab" role="tab" aria-selected="true" onclick="showTab(0)">Duration curves</button>
  <button class="tab" role="tab" aria-selected="false" onclick="showTab(1)">Surplus utilisation</button>
  <button class="tab" role="tab" aria-selected="false" onclick="showTab(2)">Long-duration storage</button>
</nav>

<section class="pp on" id="pp0">
  <h2>Load and residual-load duration curves</h2>
  <p class="lede">Demand (load duration curve), available VRE, and net load before vs after storage, each sorted from highest to lowest hour. The <b>positive top</b> is the firm-capacity requirement; the <b>negative tail</b> is surplus (VRE above demand). Storage pulls the top down (peak shaving by discharge) and lifts the tail toward zero (surplus absorbed by charging).</p>
  <div class="lg" id="lg_ldc"></div>
  <div class="grid2" id="ldcCards"></div>
</section>

<section class="pp" id="pp1">
  <h2>How the VRE surplus is utilised</h2>
  <p class="lede">Left bar: where every TWh of available VRE goes - served directly to demand, charged into batteries (short duration), converted by electrolysis into the H2 store (long duration), or curtailed. Right bar: what covers the residual demand - battery discharge, H2 turbine, gas, or unserved. Charging is attributed to VRE.</p>
  <div class="lg" id="lg_sp"></div>
  <div class="card"><div id="c_surplus"></div></div>
  <div class="card"><h3>Energy decomposition (TWh)</h3><div id="t_surplus"></div></div>
</section>

<section class="pp" id="pp2">
  <h2>Long-duration storage need</h2>
  <p class="lede">The H2 store fills through the windy/summer months and empties across winter droughts - its seasonal swing is the long-duration storage energy the system must build. The sizing sweep shows how gas and curtailment fall as the store grows.</p>
  <div class="kpi" id="kpi_lds"></div>
  <div class="grid2">
    <div class="card"><h3>H2 store state of charge (daily, GWh)</h3><div class="lg" id="lg_soc"></div><div id="c_soc"></div></div>
    <div class="card"><h3>Sizing sweep: gas &amp; curtailment vs H2 store (TWh)</h3><div class="lg" id="lg_sz"></div><div id="c_sizing"></div></div>
  </div>
</section>
</div>
<div class="tip" id="tip"></div>
<script>
const DATA=/*__DATA__*/;
const css=k=>getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const PAL=['--avail','--pre','--post','--h2','--bat','--curt'];
const scol=i=>css(PAL[i%PAL.length]);
const tip=document.getElementById('tip');
function T(e,h){tip.innerHTML=h;tip.style.opacity=1;tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-10)+'px';}
function H(){tip.style.opacity=0;}
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
function niceTicks(lo,hi,n){if(hi<=lo){hi=lo+1;}const raw=(hi-lo)/n;const mag=Math.pow(10,Math.floor(Math.log10(raw)));
  const st=[1,2,2.5,5,10].map(m=>m*mag).find(s=>s>=raw)||10*mag;const t=[];let v=Math.ceil(lo/st)*st;
  for(;v<=hi+1e-9;v+=st)t.push(Math.abs(v)<1e-9?0:+v.toFixed(6));return t;}
function legend(id,items){document.getElementById(id).innerHTML=items.map(i=>
  `<span><i class="sw" style="background:${i.c}"></i>${esc(i.t)}</span>`).join('');}

function lineChart(mount,x,series,unit,xlabel,xfmt){
  const el=document.getElementById(mount);if(!el)return;
  const W=560,Hh=300,mL=52,mR=14,mT=12,mB=34,iw=W-mL-mR,ih=Hh-mT-mB;
  const ally=[].concat(...series.map(s=>s.y));
  let lo=Math.min(0,...ally),hi=Math.max(...ally);const ticks=niceTicks(lo,hi,5);
  lo=Math.min(lo,ticks[0]);hi=Math.max(hi,ticks[ticks.length-1]);
  const xmin=Math.min(...x),xmax=Math.max(...x);
  const X=v=>mL+(v-xmin)/((xmax-xmin)||1)*iw, Y=v=>mT+(hi-v)/((hi-lo)||1)*ih;
  let s=`<svg viewBox="0 0 ${W} ${Hh}" font-family="inherit">`;
  ticks.forEach(t=>{s+=`<line x1="${mL}" y1="${Y(t).toFixed(1)}" x2="${W-mR}" y2="${Y(t).toFixed(1)}" stroke="${css('--grid')}" stroke-width="1"/>`;
    s+=`<text x="${mL-6}" y="${(Y(t)+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${css('--muted')}">${t}</text>`;});
  if(lo<0)s+=`<line x1="${mL}" y1="${Y(0).toFixed(1)}" x2="${W-mR}" y2="${Y(0).toFixed(1)}" stroke="${css('--ink2')}" stroke-width="1.1" stroke-dasharray="3 3"/>`;
  const xt=[0,0.25,0.5,0.75,1].map(f=>xmin+f*(xmax-xmin));
  xt.forEach(t=>{s+=`<text x="${X(t).toFixed(1)}" y="${Hh-12}" text-anchor="middle" font-size="11" fill="${css('--muted')}">${xfmt?xfmt(t):t}</text>`;});
  s+=`<text x="${mL+iw/2}" y="${Hh-1}" text-anchor="middle" font-size="11" fill="${css('--muted')}">${xlabel}</text>`;
  s+=`<text transform="translate(12 ${mT+ih/2}) rotate(-90)" text-anchor="middle" font-size="11" fill="${css('--muted')}">${unit}</text>`;
  series.forEach(se=>{let d='';se.y.forEach((v,i)=>{d+=(i?'L':'M')+X(x[i]).toFixed(1)+' '+Y(v).toFixed(1);});
    s+=`<path d="${d}" fill="none" stroke="${se.c}" stroke-width="1.7"/>`;});
  // hover
  s+=`<rect x="${mL}" y="${mT}" width="${iw}" height="${ih}" fill="transparent" `+
     `onmousemove="hoverLine(event,'${mount}')" onmouseleave="H()"/>`;
  s+=`<line id="g_${mount}" x1="0" y1="${mT}" x2="0" y2="${mT+ih}" stroke="${css('--ink2')}" stroke-width="1" opacity="0"/>`;
  s+='</svg>';el.innerHTML=s;
  el._chart={x,series,X,Y,mL,iw,unit,xlabel,xfmt,mT,ih};
}
function hoverLine(e,mount){const el=document.getElementById(mount),c=el._chart;if(!c)return;
  const r=el.querySelector('svg').getBoundingClientRect();const px=(e.clientX-r.left)/r.width*560;
  let bi=0,bd=1e9;c.x.forEach((xv,i)=>{const d=Math.abs(c.X(xv)-px);if(d<bd){bd=d;bi=i;}});
  const g=document.getElementById('g_'+mount);g.setAttribute('x1',c.X(c.x[bi]).toFixed(1));g.setAttribute('x2',c.X(c.x[bi]).toFixed(1));g.setAttribute('opacity','0.5');
  const lab=c.xfmt?c.xfmt(c.x[bi]):c.x[bi];
  let h=`<b>${lab}</b>`;c.series.forEach(se=>{h+=`<br><span style="color:${se.c}">&#9632;</span> ${esc(se.t)}: ${se.y[bi]} ${c.unit}`;});
  T(e,h);}

function drawLDC(){
  const cards=document.getElementById('ldcCards');cards.innerHTML='';
  DATA.scen.forEach(sc=>{cards.innerHTML+=`<div class="card"><h3>${sc.label}</h3><div id="ldc_${sc.key}"></div></div>`;});
  DATA.scen.forEach(sc=>{const L=DATA.ldc[sc.key];const gw=a=>a.map(v=>+(v/1000).toFixed(2));
    lineChart('ldc_'+sc.key,L.pct,[
      {t:'demand',c:css('--demand'),y:gw(L.demand)},
      {t:'available VRE',c:css('--avail'),y:gw(L.avail)},
      {t:'net load, pre-storage',c:css('--pre'),y:gw(L.pre)},
      {t:'net load, post-storage',c:css('--post'),y:gw(L.post)}],
      'GW','% of hours (sorted)',v=>Math.round(v)+'%');});
  legend('lg_ldc',[{t:'demand',c:css('--demand')},{t:'available VRE',c:css('--avail')},
    {t:'net load pre-storage',c:css('--pre')},{t:'net load post-storage',c:css('--post')}]);
}

function stackBar(mount,rows,unit){
  const el=document.getElementById(mount);if(!el)return;
  const W=560,rh=54,gap=26,mT=8,mL=8,mR=8,lblH=18;const Hh=mT+rows.length*(rh+gap);
  const maxTot=Math.max(...rows.map(r=>r.parts.reduce((a,p)=>a+Math.max(0,p.v),0)));
  const iw=W-mL-mR;const X=v=>v/(maxTot||1)*iw;
  let s=`<svg viewBox="0 0 ${W} ${Hh}" font-family="inherit">`;
  rows.forEach((r,ri)=>{const y0=mT+ri*(rh+gap);let x=mL;
    s+=`<text x="${mL}" y="${y0+12}" font-size="12" font-weight="600" fill="${css('--ink')}">${esc(r.label)}</text>`;
    r.parts.forEach(p=>{const w=X(Math.max(0,p.v));if(w<=0)return;
      s+=`<rect x="${x.toFixed(1)}" y="${y0+lblH}" width="${w.toFixed(1)}" height="${rh-lblH}" fill="${p.c}" `+
         `onmousemove="T(event,'${esc(p.t)}: ${p.v} ${unit}')" onmouseleave="H()"><title>${esc(p.t)}: ${p.v} ${unit}</title></rect>`;
      if(w>44)s+=`<text x="${(x+w/2).toFixed(1)}" y="${y0+lblH+(rh-lblH)/2+4}" text-anchor="middle" font-size="11" fill="#fff">${p.v}</text>`;
      x+=w;});
  });
  s+='</svg>';el.innerHTML=s;
}
function drawSurplus(){
  const rows=[],unit='TWh';
  DATA.scen.forEach(sc=>{const d=DATA.surplus[sc.key];
    rows.push({label:sc.label+' - VRE use',parts:[
      {t:'served to demand',c:css('--served'),v:d.served_twh},
      {t:'battery charge',c:css('--bat'),v:d.battery_charge_twh},
      {t:'electrolysis -> H2',c:css('--h2'),v:d.electrolysis_twh},
      {t:'curtailed',c:css('--curt'),v:d.curtail_twh}]});
    rows.push({label:sc.label+' - firm/flex supply',parts:[
      {t:'battery discharge',c:css('--bat'),v:d.battery_discharge_twh},
      {t:'H2 turbine',c:css('--h2'),v:d.h2_turbine_twh},
      {t:'gas',c:css('--gas'),v:d.gas_twh},
      {t:'unserved',c:css('--uns'),v:d.unserved_twh}]});
  });
  stackBar('c_surplus',rows,unit);
  legend('lg_sp',[{t:'served',c:css('--served')},{t:'battery',c:css('--bat')},
    {t:'H2 (electrolysis/turbine)',c:css('--h2')},{t:'curtailed',c:css('--curt')},
    {t:'gas',c:css('--gas')},{t:'unserved',c:css('--uns')}]);
  const keys=['served_twh','battery_charge_twh','electrolysis_twh','curtail_twh','total_vre_twh',
    'battery_discharge_twh','h2_turbine_twh','gas_twh','unserved_twh','firm_peak_pre_gw','firm_peak_post_gw'];
  const nm={served_twh:'VRE served',battery_charge_twh:'Battery charge',electrolysis_twh:'Electrolysis (H2)',
    curtail_twh:'Curtailed',total_vre_twh:'Total available VRE',battery_discharge_twh:'Battery discharge',
    h2_turbine_twh:'H2 turbine',gas_twh:'Gas',unserved_twh:'Unserved',firm_peak_pre_gw:'Firm peak pre (GW)',
    firm_peak_post_gw:'Firm peak post (GW)'};
  let t='<table><tr><th>metric</th>'+DATA.scen.map(s=>`<th>${s.label}</th>`).join('')+'</tr>';
  keys.forEach(k=>{t+=`<tr><td>${nm[k]}</td>`+DATA.scen.map(s=>`<td>${DATA.surplus[s.key][k]}</td>`).join('')+'</tr>';});
  t+='</table>';document.getElementById('t_surplus').innerHTML=t;
}

function drawSoc(){
  const cols={};DATA.scen.forEach((sc,i)=>cols[sc.key]=scol(i));
  const anyKey=DATA.scen[0].key;const x=DATA.soc[anyKey].day;
  lineChart('c_soc',x,DATA.scen.map(sc=>({t:sc.label,c:cols[sc.key],y:DATA.soc[sc.key].gwh})),
    'GWh','day of year',v=>Math.round(v));
  legend('lg_soc',DATA.scen.map(sc=>({t:sc.label,c:cols[sc.key]})));
}
function drawSizing(){
  const cols={};DATA.scen.forEach((sc,i)=>cols[sc.key]=scol(i));
  const series=[];DATA.scen.forEach(sc=>{const z=DATA.sizing[sc.key];
    series.push({t:sc.label+' curtailment',c:cols[sc.key],y:z.curtailment_twh,x:z.h2_store_gwh});
    series.push({t:sc.label+' gas',c:cols[sc.key],y:z.gas_twh,x:z.h2_store_gwh,dash:1});});
  // custom multi-x line chart (each series has its own x)
  const el=document.getElementById('c_sizing');const W=560,Hh=300,mL=52,mR=14,mT=12,mB=34,iw=W-mL-mR,ih=Hh-mT-mB;
  const ally=[].concat(...series.map(s=>s.y)),allx=[].concat(...series.map(s=>s.x));
  let lo=0,hi=Math.max(...ally);const ticks=niceTicks(lo,hi,5);hi=Math.max(hi,ticks[ticks.length-1]);
  const xmin=0,xmax=Math.max(...allx);const X=v=>mL+(v-xmin)/((xmax-xmin)||1)*iw,Y=v=>mT+(hi-v)/((hi-lo)||1)*ih;
  let s=`<svg viewBox="0 0 ${W} ${Hh}" font-family="inherit">`;
  ticks.forEach(t=>{s+=`<line x1="${mL}" y1="${Y(t).toFixed(1)}" x2="${W-mR}" y2="${Y(t).toFixed(1)}" stroke="${css('--grid')}"/>`;
    s+=`<text x="${mL-6}" y="${(Y(t)+3.5).toFixed(1)}" text-anchor="end" font-size="11" fill="${css('--muted')}">${t}</text>`;});
  [0,0.25,0.5,0.75,1].forEach(f=>{const t=xmin+f*(xmax-xmin);
    s+=`<text x="${X(t).toFixed(1)}" y="${Hh-12}" text-anchor="middle" font-size="11" fill="${css('--muted')}">${Math.round(t)}</text>`;});
  s+=`<text x="${mL+iw/2}" y="${Hh-1}" text-anchor="middle" font-size="11" fill="${css('--muted')}">H2 store (GWh)</text>`;
  s+=`<text transform="translate(12 ${mT+ih/2}) rotate(-90)" text-anchor="middle" font-size="11" fill="${css('--muted')}">TWh/yr</text>`;
  series.forEach(se=>{let d='';se.y.forEach((v,i)=>{d+=(i?'L':'M')+X(se.x[i]).toFixed(1)+' '+Y(v).toFixed(1);});
    s+=`<path d="${d}" fill="none" stroke="${se.c}" stroke-width="1.7" ${se.dash?'stroke-dasharray="4 3"':''}/>`;
    se.x.forEach((xv,i)=>{s+=`<circle cx="${X(xv).toFixed(1)}" cy="${Y(se.y[i]).toFixed(1)}" r="2.4" fill="${se.c}"><title>${esc(se.t)} @ ${xv} GWh: ${se.y[i]} TWh</title></circle>`;});});
  s+='</svg>';el.innerHTML=s;
  legend('lg_sz',DATA.scen.map((sc,i)=>({t:sc.label,c:cols[sc.key]})).concat([{t:'solid=curtailment, dashed=gas',c:css('--muted')}]));
  // KPIs
  let k='';DATA.scen.forEach(sc=>{const d=DATA.surplus[sc.key];
    k+=`<div><b>${d.h2_soc_swing_gwh}</b> ${sc.label} H2 swing (GWh)</div>`;
    k+=`<div><b>${d.firm_peak_pre_gw}&#8594;${d.firm_peak_post_gw}</b> ${sc.label} firm peak (GW)</div>`;});
  document.getElementById('kpi_lds').innerHTML=k;
}

function redraw(){drawLDC();drawSurplus();drawSoc();drawSizing();}
function showTab(i){document.querySelectorAll('.tab').forEach((t,k)=>t.setAttribute('aria-selected',k===i));
  document.querySelectorAll('.pp').forEach((p,k)=>p.classList.toggle('on',k===i));redraw();}
function toggleTheme(){const r=document.documentElement,cur=r.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  r.setAttribute('data-theme',cur==='dark'?'light':'dark');redraw();}
redraw();
</script></body></html>"""


def agg_generators(n):
    """Aggregate generators to one per carrier; VRE keeps available (p_max_pu*p_nom) energy."""
    g = n.generators
    pmax = n.generators_t.p_max_pu
    rows = {}          # carrier -> (p_nom, marginal_cost)
    prof = {}          # carrier -> aggregate p_max_pu series (or None -> 1.0)
    for c, idx in g.groupby('carrier').groups.items():
        if c == 'load_shedding':
            continue
        pn = g.loc[idx, 'p_nom']
        p_nom = float(pn.sum())
        if p_nom <= 0:
            continue
        mc = float((g.loc[idx, 'marginal_cost'] * pn).sum() / p_nom)
        rows[c] = (p_nom, mc)
        withp = [x for x in idx if x in pmax.columns]
        static = [x for x in idx if x not in pmax.columns]
        avail = (pmax[withp] * pn[withp]).sum(axis=1) if withp else pd.Series(0.0, index=n.snapshots)
        if static:
            avail = avail + float((g.loc[static, 'p_max_pu'] * pn[static]).sum())
        prof[c] = (avail / p_nom).clip(0, 1)
    return rows, prof


def agg_storage_units(n):
    """Aggregate storage units by carrier -> p_nom, max_hours, efficiencies."""
    out = {}
    su = n.storage_units
    for c, idx in su.groupby('carrier').groups.items():
        pn = su.loc[idx, 'p_nom']
        p_nom = float(pn.sum())
        if p_nom <= 0:
            continue
        energy = float((pn * su.loc[idx, 'max_hours']).sum())
        out[c] = dict(p_nom=p_nom, max_hours=energy / p_nom,
                      eff_store=float((su.loc[idx, 'efficiency_store'] * pn).sum() / p_nom),
                      eff_dispatch=float((su.loc[idx, 'efficiency_dispatch'] * pn).sum() / p_nom))
    return out


def h2_params(n):
    st = n.stores.loc['GB_H2_storage']
    el = n.links[n.links.carrier == 'electrolysis']
    tb = n.links[n.links.carrier == 'H2_turbine']
    return dict(e_nom=float(st.e_nom), standing_loss=float(st.get('standing_loss', 0.0)),
                el_p_nom=float(el.p_nom.sum()), el_eff=float(el.efficiency.mean()),
                tb_p_nom=float(tb.p_nom.sum()), tb_eff=float(tb.efficiency.mean()))


def build_network(snapshots, demand, gens, prof, storage, h2, h2_e_nom):
    m = pypsa.Network()
    m.set_snapshots(snapshots)
    m.add('Bus', 'GB')
    m.add('Load', 'demand', bus='GB', p_set=demand)
    for c, (p_nom, mc) in gens.items():
        kw = dict(bus='GB', carrier=c, p_nom=p_nom, marginal_cost=mc)
        if c in VRE:
            kw['p_max_pu'] = prof[c].values
        else:
            kw['p_max_pu'] = prof[c].values      # firm availability profile too
        m.add('Generator', c, **kw)
    m.add('Generator', 'unserved', bus='GB', carrier='load_shedding', p_nom=1e6, marginal_cost=VOLL)
    for c, s in storage.items():
        m.add('StorageUnit', c, bus='GB', carrier=c, p_nom=s['p_nom'], max_hours=s['max_hours'],
              efficiency_store=s['eff_store'], efficiency_dispatch=s['eff_dispatch'],
              cyclic_state_of_charge=True)
    # hydrogen: store + electrolysis (GB->H2) + turbine (H2->GB)
    m.add('Bus', 'H2')
    m.add('Store', 'GB_H2_storage', bus='H2', e_nom=h2_e_nom, e_cyclic=True,
          standing_loss=h2['standing_loss'], marginal_cost=0.0)
    m.add('Link', 'electrolysis', bus0='GB', bus1='H2', p_nom=h2['el_p_nom'], efficiency=h2['el_eff'])
    m.add('Link', 'H2_turbine', bus0='H2', bus1='GB', p_nom=h2['tb_p_nom'], efficiency=h2['tb_eff'])
    return m


def solve(m):
    try:
        m.optimize(solver_name=SOLVER, solver_options={'threads': 4})
    except Exception:
        m.optimize(solver_name='highs')
    return m


def recompute_all():
    for scn, meta in SCEN.items():
        year = meta['year']
        print(f'=== {scn} ({year} w{meta["wy"]}) ===')
        n = pypsa.Network(fr'{MK}\{scn}_wholesale.nc')
        snaps = n.snapshots
        demand = n.loads_t.p_set.sum(axis=1)
        gens, prof = agg_generators(n)
        storage = agg_storage_units(n)
        h2 = h2_params(n)
        del n
        print(f'  aggregated {len(gens)} gen carriers, {len(storage)} storage carriers; '
              f'H2 store {h2["e_nom"]/1e3:.0f} GWh, electrolyser {h2["el_p_nom"]/1e3:.1f} GW, '
              f'turbine {h2["tb_p_nom"]/1e3:.1f} GW')

        # 1) full-year solve at the model's H2 store size
        m = solve(build_network(snaps, demand, gens, prof, storage, h2, h2['e_nom']))
        soc = pd.DataFrame(index=snaps)
        soc['h2_soc_gwh'] = m.stores_t.e['GB_H2_storage'] / 1e3
        soc['electrolysis_mw'] = m.links_t.p0['electrolysis']
        soc['h2_turbine_mw'] = (m.links_t.p0['H2_turbine'] * h2['tb_eff'])
        if 'Battery' in m.storage_units.index and not m.storage_units_t.state_of_charge.empty:
            bat = [c for c in storage if c in m.storage_units_t.state_of_charge.columns]
            soc['battery_soc_gwh'] = m.storage_units_t.state_of_charge[bat].sum(axis=1) / 1e3
        soc['unserved_mw'] = m.generators_t.p['unserved']
        soc['demand_mw'] = demand.values
        soc.round(2).to_csv(fr'{OUTDIR}\seasonal_storage_{scn}_soc.csv')
        # full-year generation mix (1 generator per carrier in this aggregated network)
        mix = m.generators_t.p.copy()
        mix['H2_turbine'] = m.links_t.p0['H2_turbine'] * h2['tb_eff']
        sp = m.storage_units_t.p
        mix['storage_discharge'] = sp.clip(lower=0).sum(axis=1)
        mix['storage_charge'] = sp.clip(upper=0).sum(axis=1)
        mix['electrolysis'] = -m.links_t.p0['electrolysis']
        mix['demand'] = demand.values
        mix.round(0).to_csv(fr'{OUTDIR}\seasonal_storage_{scn}_genmix.csv')
        swing = soc['h2_soc_gwh'].max() - soc['h2_soc_gwh'].min()
        print(f'  H2 SoC seasonal swing: {swing:.0f} GWh (max {soc["h2_soc_gwh"].max():.0f} of '
              f'{h2["e_nom"]/1e3:.0f}); unserved {soc["unserved_mw"].sum()/1e6:.2f} TWh')

        # 2) sizing sweep: unserved energy vs H2 store energy
        base = h2['e_nom']
        sizes = sorted(set([0.0, 0.25*base, 0.5*base, base, 1.5*base, 2*base, 3*base, 5*base]))
        gas_car = ['CCGT', 'OCGT', 'gas_engine', 'CHP']
        vre_car = [c for c in VRE if c in gens]
        rows = []
        for e in sizes:
            ms = solve(build_network(snaps, demand, gens, prof, storage, h2, e))
            uns = ms.generators_t.p['unserved'].sum() / 1e6
            gas = ms.generators_t.p[[c for c in gas_car if c in ms.generators_t.p.columns]].sum().sum() / 1e6
            avail = sum(float((gens[c][0] * ms.generators_t.p_max_pu[c]).sum())
                        for c in vre_car if c in ms.generators_t.p_max_pu.columns)
            disp = ms.generators_t.p[[c for c in vre_car if c in ms.generators_t.p.columns]].sum().sum()
            curt = (avail - disp) / 1e6
            rows.append({'h2_store_gwh': round(e/1e3, 1), 'unserved_twh': round(uns, 3),
                         'gas_twh': round(gas, 2), 'curtailment_twh': round(curt, 2)})
            print(f'    store {e/1e3:6.0f} GWh -> gas {gas:6.2f} TWh, curtail {curt:6.2f} TWh, unserved {uns:.3f}')
        pd.DataFrame(rows).to_csv(fr'{OUTDIR}\seasonal_storage_{scn}_sizing.csv', index=False)
    print('recompute done')


# ─────────────────────────────────────────────────────────────────────────────
# Load-duration / residual-load-duration analysis (no LP solve needed)
# ─────────────────────────────────────────────────────────────────────────────
GAS_CAR = ['CCGT', 'OCGT', 'gas_engine', 'CHP']


def vre_availability(scn):
    """Per-hour available VRE (MW) = sum_c p_nom_c * p_max_pu_c over VRE carriers.

    Reads the solved wholesale network only (no LP solve); one network in memory
    at a time on this 17 GB box, released before returning.
    """
    n = pypsa.Network(fr'{MK}\{scn}_wholesale.nc')
    gens, prof = agg_generators(n)
    snaps = n.snapshots
    per = {}
    for c in VRE:
        if c in gens:
            per[c] = gens[c][0] * prof[c]      # p_nom * availability profile
    del n
    per = pd.DataFrame(per, index=snaps)
    return per.sum(axis=1), per


def compute_ldc(scn):
    """Build the duration curves + surplus/deficit decomposition for one scenario.

    Combines available VRE (from the network) with the persisted genmix (dispatch,
    storage charge/discharge, electrolysis, H2 turbine, unserved) and the soc CSV.
    Writes seasonal_storage_<scn>_ldc.csv and _surplus.csv. Assumes storage charging
    is VRE-sourced (the surplus-utilisation split attributes all charging to VRE).
    """
    avail, _ = vre_availability(scn)
    gm = pd.read_csv(fr'{OUTDIR}\seasonal_storage_{scn}_genmix.csv', index_col=0, parse_dates=True)
    gm = gm.reindex(avail.index)
    demand = gm['demand']
    vre_cols = [c for c in VRE if c in gm.columns]
    disp_vre = gm[vre_cols].clip(lower=0).sum(axis=1)
    curtail = (avail - disp_vre).clip(lower=0)
    charge_load = (-gm['storage_charge']).clip(lower=0) + (-gm['electrolysis']).clip(lower=0)
    discharge_out = gm['storage_discharge'].clip(lower=0) + gm['H2_turbine'].clip(lower=0)
    net_pre = demand - avail                       # residual before storage (neg tail = surplus)
    net_post = net_pre - (discharge_out - charge_load)   # after storage: peak shaved, surplus absorbed

    dsc = lambda s: np.sort(np.asarray(s, dtype=float))[::-1]
    ldc = pd.DataFrame({'demand_sorted': dsc(demand), 'avail_vre_sorted': dsc(avail),
                        'net_pre_sorted': dsc(net_pre), 'net_post_sorted': dsc(net_post)})
    ldc.index.name = 'rank'
    ldc.round(1).to_csv(fr'{OUTDIR}\seasonal_storage_{scn}_ldc.csv')

    twh = lambda s: float(np.asarray(s, dtype=float).sum()) / 1e6      # MWh -> TWh
    total_vre = twh(avail)
    battery_charge = twh((-gm['storage_charge']).clip(lower=0))
    electrolysis = twh((-gm['electrolysis']).clip(lower=0))
    curtail_twh = twh(curtail)
    served = total_vre - battery_charge - electrolysis - curtail_twh   # VRE straight to demand
    gas_cols = [c for c in GAS_CAR if c in gm.columns]
    soc = pd.read_csv(fr'{OUTDIR}\seasonal_storage_{scn}_soc.csv', index_col=0, parse_dates=True)
    swing = float(soc['h2_soc_gwh'].max() - soc['h2_soc_gwh'].min())
    m = SCEN[scn]
    row = dict(
        scenario=scn, year=m['year'], weather_year=m['wy'], label=_label(scn),
        demand_twh=round(twh(demand), 2),
        total_vre_twh=round(total_vre, 2), served_twh=round(served, 2),
        battery_charge_twh=round(battery_charge, 2), electrolysis_twh=round(electrolysis, 2),
        curtail_twh=round(curtail_twh, 2),
        firm_peak_pre_gw=round(float(net_pre.max()) / 1e3, 2),
        firm_peak_post_gw=round(float(net_post.max()) / 1e3, 2),
        battery_discharge_twh=round(twh(gm['storage_discharge'].clip(lower=0)), 2),
        h2_turbine_twh=round(twh(gm['H2_turbine'].clip(lower=0)), 2),
        gas_twh=round(twh(gm[gas_cols].clip(lower=0)) if gas_cols else 0.0, 2),
        unserved_twh=round(twh(gm['unserved'].clip(lower=0)), 3),
        h2_soc_swing_gwh=round(swing, 0),
        h2_in_twh=round(electrolysis, 2), h2_out_twh=round(twh(gm['H2_turbine'].clip(lower=0)), 2))
    pd.DataFrame([row]).to_csv(fr'{OUTDIR}\seasonal_storage_{scn}_surplus.csv', index=False)
    # sanity: surplus split must sum to total available VRE
    split = served + battery_charge + electrolysis + curtail_twh
    print(f'  {scn} ({_label(scn)}): VRE {total_vre:.1f} TWh = served {served:.1f} + battery {battery_charge:.1f} '
          f'+ H2 {electrolysis:.1f} + curtailed {curtail_twh:.1f} TWh (split sum {split:.1f}, '
          f'err {abs(split-total_vre):.3f})')
    print(f'    firm peak {row["firm_peak_pre_gw"]:.1f} -> {row["firm_peak_post_gw"]:.1f} GW after storage; '
          f'H2 SoC swing {swing:.0f} GWh; unserved {row["unserved_twh"]:.3f} TWh')
    return ldc, row, soc


def build_report():
    """Assemble ONE Excel workbook + ONE tabbed SVG HTML from the persisted CSVs."""
    ldcs, surplus, socs, sizings = {}, {}, {}, {}
    for scn in SCEN:
        ldcs[scn], surplus[scn], socs[scn] = compute_ldc(scn)
        sizings[scn] = pd.read_csv(fr'{OUTDIR}\seasonal_storage_{scn}_sizing.csv')
    surplus_df = pd.DataFrame([surplus[scn] for scn in SCEN])

    # ---- Excel (README first) ----
    tag = lambda s: _label(s).replace(' ', '_')      # e.g. 2030_w2010 -> safe sheet name
    xlsx = fr'{OUTDIR}\seasonal_storage_analysis.xlsx'
    sheets = {
        'README': pd.DataFrame({
            'sheet': ['Surplus_split', 'SoC_summary'] +
                     [f'LDC_{tag(s)}' for s in SCEN] + [f'Sizing_{tag(s)}' for s in SCEN],
            'contents': [
                'Per-scenario energy decomposition: available VRE -> served/battery/H2/curtailed (TWh); '
                'deficit met by battery/H2 turbine/gas/unserved; firm peak pre/post storage; H2 SoC swing. '
                'Scenarios are horizon x weather year (2030/2040 x w2010/w2013).',
                'H2 store seasonal swing (GWh) and throughput (TWh in/out) per scenario'] +
                [f'Duration curves for {_label(s)}: demand, available VRE, net load pre- & post-storage, '
                 f'each sorted descending over the year (neg net = VRE surplus)' for s in SCEN] +
                [f'Sizing sweep for {_label(s)}: H2 store GWh -> unserved/gas/curtailment TWh' for s in SCEN]}),
        'Surplus_split': surplus_df,
        'SoC_summary': surplus_df[['scenario', 'year', 'weather_year', 'h2_soc_swing_gwh', 'h2_in_twh', 'h2_out_twh']],
    }
    for scn in SCEN:
        sheets[f'LDC_{tag(scn)}'] = ldcs[scn].reset_index()
        sheets[f'Sizing_{tag(scn)}'] = sizings[scn]
    with pd.ExcelWriter(xlsx, engine='openpyxl') as xw:
        for name, df in sheets.items():
            (df if not df.empty else pd.DataFrame({'note': ['no data']})).to_excel(
                xw, sheet_name=name[:31], index=False)
    print(f'wrote {xlsx} ({len(sheets)} sheets)')

    # ---- HTML (embed downsampled DATA; full data stays in Excel/CSV) ----
    def samp(a, n=720):
        a = np.asarray(a, dtype=float)
        idx = np.linspace(0, len(a) - 1, min(n, len(a))).round().astype(int)
        return [round(float(x), 1) for x in a[idx]]

    data = {'scen': [{'key': scn, 'year': SCEN[scn]['year'], 'wy': SCEN[scn]['wy'], 'label': _label(scn)}
                     for scn in SCEN], 'ldc': {}, 'surplus': {}, 'soc': {}, 'sizing': {}}
    for scn in SCEN:
        L = ldcs[scn]
        data['ldc'][scn] = {'pct': [round(100 * i / (len(L) - 1), 2) for i in
                                    np.linspace(0, len(L) - 1, min(720, len(L))).round().astype(int)],
                            'demand': samp(L['demand_sorted']), 'avail': samp(L['avail_vre_sorted']),
                            'pre': samp(L['net_pre_sorted']), 'post': samp(L['net_post_sorted'])}
        data['surplus'][scn] = surplus[scn]
        so = socs[scn]['h2_soc_gwh'].resample('1D').mean()
        data['soc'][scn] = {'day': list(range(len(so))), 'gwh': [round(float(x), 0) for x in so.values]}
        sz = sizings[scn]
        data['sizing'][scn] = {k: [round(float(x), 2) for x in sz[k].values]
                               for k in ['h2_store_gwh', 'unserved_twh', 'gas_twh', 'curtailment_twh']}

    html = _HTML_TMPL.replace('/*__DATA__*/', json.dumps(data))
    out = fr'{OUTDIR}\seasonal_storage_report.html'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'wrote {out}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--recompute', action='store_true',
                    help='re-solve the full-year LP + sizing sweep before building the report (slow)')
    args = ap.parse_args()
    if args.recompute:
        recompute_all()
    build_report()
    print('DONE')
