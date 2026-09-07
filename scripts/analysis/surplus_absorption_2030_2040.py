#!/usr/bin/env python3
"""
Surplus absorption and renewable curtailment, FES 2025 HT 2030 vs 2040.

Research question: how much VRE surplus does the modelled GB system absorb, which
technologies absorb it, and how much is curtailed? Quantifies the role of the
FES-planned hydrogen fleet, whose capacity dominates storage energy volume.

Reads the persistent wholesale-stage outputs of the two scenarios the RDC and
dispatch figures use (HT30_disp_w2010 / HT40_disp_w2010). It never re-solves, so
there is no --recompute flag: the .nc and hourly CSVs in resources/market ARE the
cached model results.

IMPORTANT - scenario choice: scripts/analysis/plot_rdc_combined.R uses
*_disp_w2010. The *_market_w2010 runs are a different two-stage market simulation
and give materially different hydrogen flows. Keep this script on the same
scenarios as the figures.

Outputs (one file per medium, per the analysis-consolidation convention):
  resources/analysis/surplus_absorption_2030_2040.xlsx   multi-sheet workbook
  resources/analysis/surplus_absorption_2030_2040.html   tabbed self-contained report

Usage:
  python scripts/analysis/surplus_absorption_2030_2040.py
"""

import argparse
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

ROOT = Path(__file__).resolve().parents[2]
MARKET = ROOT / "resources" / "market"
FES_DIR = ROOT / "resources" / "FES"
OUT = ROOT / "resources" / "analysis"

SCENARIOS = {2030: "HT30_disp_w2010", 2040: "HT40_disp_w2010"}
FES_PATHWAY = "Holistic Transition"

# Efficiencies as built by scripts/hydrogen/add_hydrogen_system.py. The links CSV
# stores only p0, so H2 turbine electrical output = p0 * efficiency.
H2_TURBINE_EFF = 0.50
ELECTROLYSIS_EFF = 0.70

# Storage technology grouping, matched on component NAME to mirror the STO_P
# patterns in plot_rdc_combined.R so the two agree.
STO_PATTERNS = [("Battery", r"Battery"),
                ("Pumped hydro", r"Pumped[ _]Storage"),
                ("LAES", r"LAES")]
STO_ORDER = ["Battery", "Pumped hydro", "LAES", "Hydrogen"]

VRE_KEYS = ("wind", "solar", "pv")


def git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=str(ROOT), text=True).strip()
    except Exception:
        return "unknown"


def classify_storage(columns):
    """Assign each storage column to the first matching technology pattern."""
    out = {}
    for c in columns:
        for label, pat in STO_PATTERNS:
            if re.search(pat, c):
                out[c] = label
                break
    return out


def vre_carriers(network):
    return [c for c in network.generators.carrier.unique()
            if any(k in str(c).lower() for k in VRE_KEYS)]


def curtailment(network, dispatch, demand_GWh):
    """VRE energy available, generated and curtailed, per carrier and in total.

    "Available" is the weather-driven ceiling (p_max_pu x p_nom); "generated" is
    what the optimiser actually dispatched. The gap is curtailment. Generated
    energy is also expressed as a share of total GB demand, which is the sense in
    which the output is "used": every dispatched MWh serves load, charges storage
    or leaves via an interconnector, since the model has no other sink.
    """
    gen = network.generators
    ids = gen.index[gen.carrier.isin(vre_carriers(network))]
    pmax = network.generators_t.p_max_pu.reindex(columns=ids).dropna(axis=1, how="all")
    n_snap = len(network.snapshots)

    def avail_for(subset):
        var = [i for i in subset if i in pmax.columns]
        const = [i for i in subset if i not in pmax.columns]
        a = pmax[var].mul(gen.loc[var, "p_nom"], axis=1).values.sum() if var else 0.0
        a += gen.loc[const, "p_nom"].sum() * n_snap if const else 0.0
        return a

    def row_for(label, subset):
        a = avail_for(subset)
        d = dispatch[[c for c in dispatch.columns if c in set(subset)]].values.sum()
        cap = gen.loc[subset, "p_nom"].sum()
        return {"carrier": label, "capacity_GW": cap / 1e3,
                "available_GWh": a / 1e3, "generated_GWh": d / 1e3,
                "curtailed_GWh": (a - d) / 1e3,
                "curtailed_pct": 100 * (a - d) / a if a else np.nan,
                "capacity_factor_pct": 100 * d / (cap * n_snap) if cap else np.nan,
                "share_of_demand_pct": 100 * (d / 1e3) / demand_GWh if demand_GWh else np.nan}

    rows = []
    for carrier in sorted(gen.loc[ids, "carrier"].unique()):
        sub = gen.index[gen.carrier == carrier]
        if avail_for(sub) <= 0:
            continue
        rows.append(row_for(carrier, sub))
    rows.append(row_for("TOTAL VRE", ids))
    return pd.DataFrame(rows)


def energy_balance(network, dispatch, storage, links, demand_GWh):
    """GB energy balance: where generated energy goes.

    Generators sitting on the HVDC_External_* buses carry the EU_import carrier
    and represent foreign supply, not GB generation. They must be excluded from
    GB output and counted instead as interconnector imports, otherwise the
    balance overstates GB generation by the import volume. Interconnector p0 is
    measured at bus0 (the GB end), so p0 < 0 is an inflow to GB; the delivered
    energy applies the DC link efficiency.
    """
    gen = network.generators
    ext_buses = set(network.buses.index[
        network.buses.index.str.contains("External", case=False)])
    gb = [c for c in dispatch.columns
          if c in gen.index and gen.at[c, "bus"] not in ext_buses]
    vre_ids = set(gen.index[gen.carrier.isin(vre_carriers(network))])
    vre = dispatch[[c for c in gb if c in vre_ids]].values.sum() / 1e3
    other = dispatch[[c for c in gb if c not in vre_ids]].values.sum() / 1e3

    su_cols = [c for c in storage.columns if c in network.storage_units.index]
    net_sto = storage[su_cols].sum(axis=1)
    sto_charge = -net_sto.clip(upper=0).sum() / 1e3
    sto_discharge = net_sto.clip(lower=0).sum() / 1e3

    ely = [c for c in links.columns if c.startswith("electrolysis")]
    h2t = [c for c in links.columns if c.startswith("H2_turbine")]
    h2_in = links[ely].values.sum() / 1e3
    h2_out = links[h2t].values.sum() * H2_TURBINE_EFF / 1e3

    ic = [c for c in links.columns if c.startswith("IC_")]
    dc_eff = float(network.links.loc[ic, "efficiency"].mean()) if ic else 1.0
    imports = -links[ic].values.sum() / 1e3 * dc_eff if ic else 0.0

    charge = sto_charge + h2_in
    discharge = sto_discharge + h2_out
    residual = vre + other + imports + discharge - charge - demand_GWh
    return pd.DataFrame([
        {"item": "GB demand", "GWh": demand_GWh},
        {"item": "GB VRE generated", "GWh": vre},
        {"item": "GB non-VRE generated", "GWh": other},
        {"item": "Net interconnector imports", "GWh": imports},
        {"item": "Storage discharge", "GWh": discharge},
        {"item": "Storage charge", "GWh": -charge},
        {"item": "Storage round-trip loss", "GWh": -(charge - discharge)},
        {"item": "Residual (AC network losses)", "GWh": residual},
    ])


def storage_flows(network, storage, links):
    """Charge/discharge per technology on the FLEET-NET-per-hour basis.

    plot_rdc_combined.R sums each technology's units into one hourly net series
    before splitting into charge and discharge, so simultaneous charging and
    discharging within a fleet cancels. Reproduced here so the numbers match the
    table drawn on the figure.
    """
    labels = classify_storage([c for c in storage.columns
                               if c in network.storage_units.index])
    rows = []
    for tech in ["Battery", "Pumped hydro", "LAES"]:
        cols = [c for c, v in labels.items() if v == tech]
        if not cols:
            continue
        net = storage[cols].sum(axis=1)
        rows.append({"technology": tech,
                     "charge_GWh": -net.clip(upper=0).sum() / 1e3,
                     "discharge_GWh": net.clip(lower=0).sum() / 1e3})

    ely = [c for c in links.columns if c.startswith("electrolysis")]
    h2t = [c for c in links.columns if c.startswith("H2_turbine")]
    elec_in = links[ely].sum(axis=1)
    elec_out = links[h2t].sum(axis=1) * H2_TURBINE_EFF
    net_h2 = elec_out - elec_in
    rows.append({"technology": "Hydrogen",
                 "charge_GWh": -net_h2.clip(upper=0).sum() / 1e3,
                 "discharge_GWh": net_h2.clip(lower=0).sum() / 1e3})

    df = (pd.DataFrame(rows).set_index("technology")
          .reindex(STO_ORDER).dropna(how="all").reset_index())
    total_c = df["charge_GWh"].sum()
    total_d = df["discharge_GWh"].sum()
    df["share_of_charging_pct"] = 100 * df["charge_GWh"] / total_c
    df["round_trip_pct"] = 100 * df["discharge_GWh"] / df["charge_GWh"].replace(0, np.nan)
    df.loc[len(df)] = ["TOTAL", total_c, total_d, 100.0, 100 * total_d / total_c]
    return df


def storage_capacity(network):
    """Power and energy capacity per technology, including the H2 store."""
    su = network.storage_units
    rows = []
    for carrier, grp in su.groupby("carrier"):
        rows.append({"technology": str(carrier),
                     "power_GW": grp.p_nom.sum() / 1e3,
                     "energy_GWh": (grp.p_nom * grp.max_hours).sum() / 1e3})
    links = network.links
    ely = links[links.carrier == "electrolysis"].p_nom.sum()
    turb = links[links.carrier == "H2_turbine"].p_nom.sum()
    store = network.stores.e_nom.sum()
    rows.append({"technology": "Hydrogen (store)", "power_GW": ely / 1e3,
                 "energy_GWh": store / 1e3})
    df = pd.DataFrame(rows)
    df["share_of_energy_pct"] = 100 * df["energy_GWh"] / df["energy_GWh"].sum()
    df = df.sort_values("energy_GWh", ascending=False).reset_index(drop=True)
    meta = {"electrolysis_GW": ely / 1e3, "h2_turbine_GW": turb / 1e3,
            "h2_store_GWh": store / 1e3,
            "h2_store_hours_of_turbine": store / turb if turb else np.nan}
    return df, meta


def extendability(network):
    rows = []
    for comp, attr in [("generators", "p_nom_extendable"),
                       ("storage_units", "p_nom_extendable"),
                       ("links", "p_nom_extendable"),
                       ("stores", "e_nom_extendable")]:
        df = getattr(network, comp)
        n_ext = int(df[attr].astype(bool).sum()) if len(df) and attr in df.columns else 0
        rows.append({"component": comp, "count": len(df), "extendable": n_ext})
    return pd.DataFrame(rows)


def electrolysis_utilisation(network, links):
    """Gross electrolysis load and capacity factor."""
    ely = [c for c in links.columns if c.startswith("electrolysis")]
    cap = network.links[network.links.carrier == "electrolysis"].p_nom.sum()
    used = links[ely].clip(lower=0).values.sum()
    ceiling = cap * len(network.snapshots)
    return {"electrolysis_load_GWh": used / 1e3,
            "electrolysis_max_GWh": ceiling / 1e3,
            "electrolysis_cf_pct": 100 * used / ceiling if ceiling else np.nan}


def fes_hydrogen(model_caps):
    """FES planned hydrogen capacity by vintage, against what the model built.

    rules/hydrogen.smk hardcodes FES_2024_data.csv for electrolysis regardless of
    the scenario's FES_year, so the two vintages are both reported here.
    """
    rows = []
    for vintage in (2024, 2025):
        path = FES_DIR / f"FES_{vintage}_data.csv"
        if not path.exists():
            continue
        fes = pd.read_csv(path)
        sub = fes[fes["FES Pathway"] == FES_PATHWAY]
        for detail, label in [("Hydrogen electrolysis", "electrolysis"),
                              ("Hydrogen", "H2 generation")]:
            d = sub[sub["Technology Detail"] == detail]
            if d.empty:
                continue
            row = {"source": f"FES {vintage}", "component": label}
            for yr in (2030, 2040):
                col = str(yr)
                row[f"{yr}_GW"] = d[col].sum() / 1e3 if col in d.columns else np.nan
            rows.append(row)
    built = []
    for label, key in [("electrolysis", "electrolysis_GW"),
                       ("H2 generation", "h2_turbine_GW")]:
        r = {"source": "MODEL (built)", "component": label}
        for yr in (2030, 2040):
            r[f"{yr}_GW"] = model_caps[yr][key]
        built.append(r)
    return pd.concat([pd.DataFrame(rows), pd.DataFrame(built)], ignore_index=True)


def build():
    results, caps_meta = {}, {}
    for year, scn in SCENARIOS.items():
        n = pypsa.Network(str(MARKET / f"{scn}_wholesale.nc"))
        disp = pd.read_csv(MARKET / f"{scn}_wholesale_dispatch.csv", index_col=0)
        sto = pd.read_csv(MARKET / f"{scn}_wholesale_storage.csv", index_col=0)
        lnk = pd.read_csv(MARKET / f"{scn}_wholesale_links.csv", index_col=0)

        cap_df, meta = storage_capacity(n)
        meta.update(electrolysis_utilisation(n, lnk))
        demand_GWh = n.loads_t.p_set.values.sum() / 1e3
        meta["demand_GWh"] = demand_GWh
        caps_meta[year] = meta
        results[year] = {
            "scenario": scn,
            "snapshots": len(n.snapshots),
            "h2_loads": int((n.loads.bus == "GB_H2").sum()),
            "curtailment": curtailment(n, disp, demand_GWh),
            "balance": energy_balance(n, disp, sto, lnk, demand_GWh),
            "flows": storage_flows(n, sto, lnk),
            "capacity": cap_df,
            "extendability": extendability(n),
            "meta": meta,
        }
    results["fes"] = fes_hydrogen(caps_meta)
    return results


def write_excel(res, path):
    readme = pd.DataFrame([
        ("Question", "Surplus absorption and VRE curtailment, FES 2025 HT, 2030 vs 2040"),
        ("Producing script", "scripts/analysis/surplus_absorption_2030_2040.py"),
        ("Git commit", git_commit()),
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Scenarios", ", ".join(SCENARIOS.values()) + " (wholesale stage)"),
        ("Basis", "Same scenarios and fleet-net convention as plot_rdc_combined.R"),
        ("Sheet VRE_YYYY", "VRE capacity, available, generated, curtailed, CF, share of demand"),
        ("Sheet Balance_YYYY", "Where generated energy goes: demand, storage losses, net exchange"),
        ("Sheet Flows_YYYY", "Storage charge/discharge per technology, fleet net per hour"),
        ("Sheet Capacity_YYYY", "Power and energy capacity per technology incl. H2 store"),
        ("Sheet Extendability_YYYY", "Count of extendable components (all zero = dispatch only)"),
        ("Sheet FES_hydrogen", "FES 2024 vs 2025 planned H2 capacity vs what the model built"),
        ("Caveat 1", "rules/hydrogen.smk hardcodes FES_2024_data.csv for electrolysis"),
        ("Caveat 2", "H2 store volume = 168h x turbine capacity, a model assumption not FES"),
        ("Caveat 3", "Zero loads on the GB_H2 bus: no hydrogen demand outside the power sector"),
    ], columns=["field", "value"])

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        readme.to_excel(xl, sheet_name="README", index=False)
        for year in SCENARIOS:
            r = res[year]
            r["curtailment"].to_excel(xl, sheet_name=f"VRE_{year}", index=False)
            r["balance"].to_excel(xl, sheet_name=f"Balance_{year}", index=False)
            r["flows"].to_excel(xl, sheet_name=f"Flows_{year}", index=False)
            r["capacity"].to_excel(xl, sheet_name=f"Capacity_{year}", index=False)
            r["extendability"].to_excel(xl, sheet_name=f"Extendability_{year}", index=False)
        res["fes"].to_excel(xl, sheet_name="FES_hydrogen", index=False)


HTML_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>Surplus absorption 2030 vs 2040</title><style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e2e2e2;--accent:#22305f;--hi:#f6f8fc}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8e8e8;--mut:#9aa0a6;--line:#2c2f36;--accent:#8fa4dd;--hi:#1b1f27}}
*{box-sizing:border-box}
body{margin:0 auto;padding:2rem;background:var(--bg);color:var(--fg);max-width:1100px;
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:1.4rem;margin:0 0 .3rem}
.sub{color:var(--mut);margin-bottom:1.4rem;font-size:.86rem}
.tabs{display:flex;gap:.4rem;border-bottom:2px solid var(--line);margin-bottom:1.2rem;flex-wrap:wrap}
.tab{padding:.5rem .95rem;cursor:pointer;border:0;background:none;color:var(--mut);
font-size:.9rem;border-bottom:2px solid transparent;margin-bottom:-2px}
.tab.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
table{border-collapse:collapse;width:100%;margin:.6rem 0 1.6rem;font-size:.85rem}
th,td{padding:.42rem .6rem;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:600;white-space:nowrap}
tr:last-child td{font-weight:600;background:var(--hi)}
h2{font-size:1rem;margin:1.4rem 0 .2rem}
.note{color:var(--mut);font-size:.82rem;margin:.2rem 0 .8rem}
.wrap{overflow-x:auto}
</style></head><body>
<h1>Surplus absorption and curtailment, 2030 vs 2040</h1>
<div class="sub" id="prov"></div>
<div class="tabs" id="tabs"></div><div id="body"></div>
<script>
const DATA=__DATA__;
const F=(v,d)=>(v==null||isNaN(v))?"-":Number(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
function tbl(rows,cols,hdr){
  if(!rows||!rows.length)return"";
  let h="<div class='wrap'><table><thead><tr>"+hdr.map(x=>"<th>"+x+"</th>").join("")+"</tr></thead><tbody>";
  for(const r of rows){
    h+="<tr>"+cols.map(c=>{const v=r[c];
      return "<td>"+(typeof v==="number"?F(v,c.indexOf("pct")>=0?2:1):(v==null?"-":v))+"</td>";
    }).join("")+"</tr>";
  }
  return h+"</tbody></table></div>";
}
function yearView(y){
  const d=DATA.years[y];if(!d)return"<p>no data</p>";const m=d.meta;
  return "<h2>VRE generated and curtailed</h2><p class='note'>Scenario "+d.scenario
  +". Demand "+F(m.demand_GWh,0)+" GWh.</p>"
  +tbl(d.curtailment,["carrier","capacity_GW","available_GWh","generated_GWh","curtailed_GWh","curtailed_pct","capacity_factor_pct","share_of_demand_pct"],
       ["Carrier","Capacity GW","Available GWh","Generated GWh","Curtailed GWh","Curtailed %","Capacity factor %","Share of demand %"])
  +"<h2>Energy balance</h2><p class='note'>Negative entries are sinks.</p>"
  +tbl(d.balance,["item","GWh"],["Item","GWh"])
  +"<h2>Storage flows</h2><p class='note'>Fleet net per hour, matching the table drawn on rdc_combined.</p>"
  +tbl(d.flows,["technology","charge_GWh","discharge_GWh","share_of_charging_pct","round_trip_pct"],
       ["Technology","Charge GWh","Discharge GWh","Share of charging %","Round trip %"])
  +"<h2>Storage capacity</h2><p class='note'>Electrolysis "+F(m.electrolysis_GW,2)+" GW in, H2 turbine "
  +F(m.h2_turbine_GW,2)+" GW out, H2 store "+F(m.h2_store_GWh,1)+" GWh ("+F(m.h2_store_hours_of_turbine,0)
  +" h of turbine output, a model assumption). Electrolysis load "+F(m.electrolysis_load_GWh,0)
  +" GWh at "+F(m.electrolysis_cf_pct,1)+"% capacity factor. Loads on the GB_H2 bus: "+d.h2_loads+".</p>"
  +tbl(d.capacity,["technology","power_GW","energy_GWh","share_of_energy_pct"],
       ["Technology","Power GW","Energy GWh","Share of energy %"])
  +"<h2>Extendability</h2><p class='note'>All zero confirms least-cost dispatch over a fixed FES fleet, not capacity expansion.</p>"
  +tbl(d.extendability,["component","count","extendable"],["Component","Count","Extendable"]);
}
function fesView(){
  return "<h2>FES planned hydrogen versus the model</h2>"
  +"<p class='note'>rules/hydrogen.smk hardcodes FES_2024_data.csv for electrolysis, so the built "
  +"electrolysis capacity follows FES 2024 even though the scenarios declare FES_year 2025. "
  +"H2 turbines follow FES 2025.</p>"
  +tbl(DATA.fes,["source","component","2030_GW","2040_GW"],["Source","Component","2030 GW","2040 GW"]);
}
const TABS=Object.keys(DATA.years).map(y=>[y,y]).concat([["fes","FES hydrogen"]]);
const tb=document.getElementById("tabs"),bd=document.getElementById("body");
document.getElementById("prov").textContent="Generated "+DATA.generated+" | commit "+DATA.commit
  +" | scripts/analysis/surplus_absorption_2030_2040.py";
TABS.forEach(function(t,i){
  const b=document.createElement("button");
  b.className="tab"+(i===0?" on":"");b.textContent=t[1];
  b.onclick=function(){
    Array.prototype.forEach.call(tb.children,function(c){c.classList.remove("on")});
    b.classList.add("on");
    bd.innerHTML=(t[0]==="fes")?fesView():yearView(t[0]);
  };
  tb.appendChild(b);
});
bd.innerHTML=yearView(TABS[0][0]);
</script></body></html>"""


def write_html(res, path):
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "commit": git_commit(), "years": {}}

    def clean(v):
        if v is None:
            return None
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            return None
        return round(float(v), 2)

    for year in SCENARIOS:
        r = res[year]
        payload["years"][str(year)] = {
            "scenario": r["scenario"],
            "curtailment": r["curtailment"].round(2).replace({np.nan: None}).to_dict("records"),
            "balance": r["balance"].round(2).to_dict("records"),
            "flows": r["flows"].round(2).replace({np.nan: None}).to_dict("records"),
            "capacity": r["capacity"].round(2).to_dict("records"),
            "extendability": r["extendability"].to_dict("records"),
            "meta": {k: clean(v) for k, v in r["meta"].items()},
            "h2_loads": r["h2_loads"],
        }
    payload["fes"] = res["fes"].round(2).replace({np.nan: None}).to_dict("records")
    path.write_text(HTML_TEMPLATE.replace("__DATA__", json.dumps(payload)),
                    encoding="utf-8")


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    res = build()
    xlsx = OUT / "surplus_absorption_2030_2040.xlsx"
    html = OUT / "surplus_absorption_2030_2040.html"
    write_excel(res, xlsx)
    write_html(res, html)
    for year in SCENARIOS:
        c = res[year]["curtailment"].iloc[-1]
        f = res[year]["flows"]
        h2 = f[f.technology == "Hydrogen"].iloc[0]
        print(f"{year}: curtailed {c.curtailed_GWh:,.0f} GWh ({c.curtailed_pct:.2f}%) | "
              f"H2 charge {h2.charge_GWh:,.0f} GWh "
              f"({h2.share_of_charging_pct:.1f}% of charging)")
    print(f"wrote {xlsx}")
    print(f"wrote {html}")


if __name__ == "__main__":
    main()
