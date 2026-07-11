"""
Full-year seasonal-storage LP across 41 weather years (1985-2025), FES 2030 & 2040.

The base seasonal_storage_lp.py reads one *solved* wholesale network per weather
year, of which only 2010 and 2013 exist. This driver instead runs the SAME
single-node LP against every weather year on disk, by swapping in:
  * weather-year VRE  = gap-layer national fleet-CF (2084-site REPD fleet) x FES
    capacity for wind_onshore / wind_offshore / solar_pv, and
  * weather-year demand = temperature-driven project_demand (central case),
    reconciled to the FES annual total,
while keeping the FIRM + storage + H2 fleet fixed from a reference solved network
(HT30 for 2030, HT40 for 2040). Firm availability is held weather-independent
(constant = reference mean), which is correct for thermal/nuclear/imports.

This is the OPTIMISATION (dispatch + seasonal storage), not the offline gap
residual - so it yields a full hourly generation profile per (year, weather year).

Data lives in the main checkout (cutouts, resources); the LP helpers come from the
worktree copy of seasonal_storage_lp.py. Both are wired onto sys.path below.

Outputs (resources/analysis/, in the main checkout):
  seasonal_storage_wy_summary.csv           one row per (target_year, weather_year)
  weatheryears/<ty>_wy<wy>_genmix.parquet    hourly generation-by-carrier per run

Run:  python scripts/analysis/seasonal_storage_weatheryears.py            # all 41 years
      python scripts/analysis/seasonal_storage_weatheryears.py 2010 2013  # a subset
"""
import os, sys, time, warnings
_HERE = os.path.dirname(os.path.abspath(__file__))              # worktree scripts/analysis
_MAIN = r'C:\models\pypsa-gb'                                   # data + gap layer live here
for _p in (os.path.join(_MAIN, 'scripts', 'gap'), os.path.join(_MAIN, 'scripts'), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import numpy as np, pandas as pd, pypsa
warnings.filterwarnings('ignore')

from seasonal_storage_lp import (agg_generators, agg_storage_units, h2_params,
                                 build_network, solve, VRE, GAS_CAR, MK, OUTDIR)
from build_vre_profiles import (load_repd_sites, national_fleet_cf, apply_performance_factor,
                                national_capacity_mw, filter_fes_scenario, TECH_BUILDING_BLOCKS)
from run_analysis import FES_DATA_CSV, cutout_path, load_performance_factors, load_espeni_hourly
from demand_weather_model import (national_temperature, daily_degree_days, fit_temperature_response,
                                  hour_of_week_shape, project_demand, ELECTRIFICATION_CASES)
from map_renewable_profiles import RenewableProfileGenerator
from load import get_ed1_consumer_demand_gwh

REF = {2030: 'HT30_market_w2010', 2040: 'HT40_market_w2010'}   # fleet source (weather-agnostic)
TARGET_YEARS = [2030, 2040]
WYEARS = list(range(1985, 2026))
FES_YEAR, FES_SCEN, BASE_FIT_YEAR = 2025, 'Holistic Transition', 2019
WYDIR = os.path.join(OUTDIR, 'weatheryears')


def reference_fleet(ty):
    """Firm gens (p_nom, mc), constant firm availability, storage, H2 from the reference net."""
    n = pypsa.Network(fr'{MK}\{REF[ty]}_wholesale.nc')
    gens, prof = agg_generators(n)
    firm = {c: v for c, v in gens.items() if c not in VRE}          # non-VRE carriers
    minor_vre = {c: v for c, v in gens.items() if c in VRE and c not in TECH_BUILDING_BLOCKS}
    firm_avail = {c: float(prof[c].mean()) for c in list(firm) + list(minor_vre)}   # weather-independent
    storage = agg_storage_units(n)
    h2 = h2_params(n)
    del n
    return firm, minor_vre, firm_avail, storage, h2


def run_one(ty, cf, firm, minor_vre, firm_avail, storage, h2, params, shape, temp, fes):
    """Build and solve the single-node LP for one (target year, weather year)."""
    annual_gwh = get_ed1_consumer_demand_gwh(FES_YEAR, FES_SCEN, ty, None)
    cc = ELECTRIFICATION_CASES['central']
    demand = project_demand(temp, params, shape, annual_gwh, cc['heat_mult'], cc['total_mult'])
    snaps = demand.index
    gens, prof = {}, {}
    for c_, (p_nom, mc) in {**firm, **minor_vre}.items():          # firm + minor VRE: constant avail
        gens[c_] = (p_nom, mc); prof[c_] = pd.Series(firm_avail[c_], index=snaps)
    for tech, bbs in TECH_BUILDING_BLOCKS.items():                 # 3 weather-driven VRE techs
        cap = national_capacity_mw(fes, bbs, ty)
        gens[tech] = (cap, 0.0)
        prof[tech] = cf[tech].reindex(snaps).interpolate().bfill().ffill().clip(0, 1)
    m = solve(build_network(snaps, demand, gens, prof, storage, h2, h2['e_nom']))
    return m, demand, snaps


def summarise(m, demand, snaps, ty, wy, h2):
    gp = m.generators_t.p
    twh = lambda s: float(np.asarray(s, dtype=float).sum()) / 1e6
    vre_cols = [c for c in VRE if c in gp.columns]
    avail = sum(float((m.generators.at[c, 'p_nom'] * m.generators_t.p_max_pu[c]).sum())
                for c in vre_cols if c in m.generators_t.p_max_pu.columns) / 1e6
    disp_vre = twh(gp[vre_cols].clip(lower=0)) if vre_cols else 0.0
    gas = [c for c in GAS_CAR if c in gp.columns]
    soc = m.stores_t.e['GB_H2_storage'] / 1e3
    el = m.links_t.p0['electrolysis']; tb = m.links_t.p0['H2_turbine'] * h2['tb_eff']   # p0>0 = GB->H2 draw
    sp = m.storage_units_t.p
    row = dict(target_year=ty, weather_year=wy, demand_twh=round(twh(demand), 2),
               vre_avail_twh=round(avail, 2), vre_used_twh=round(disp_vre, 2),
               curtail_twh=round(avail - disp_vre, 2),
               electrolysis_twh=round(twh(el.clip(lower=0)), 2),
               battery_charge_twh=round(twh(-sp.clip(upper=0).sum(axis=1)), 2),
               h2_turbine_twh=round(twh(tb.clip(lower=0)), 2),
               gas_twh=round(twh(gp[gas].clip(lower=0)) if gas else 0.0, 3),
               unserved_twh=round(twh(gp['unserved'].clip(lower=0)), 4),
               h2_soc_swing_gwh=round(float(soc.max() - soc.min()), 0),
               peak_demand_gw=round(float(demand.max()) / 1e3, 2))
    prof = gp.copy()
    prof['H2_turbine'] = tb.values
    prof['storage_discharge'] = sp.clip(lower=0).sum(axis=1).values
    prof['storage_charge'] = sp.clip(upper=0).sum(axis=1).values
    prof['electrolysis'] = -m.links_t.p0['electrolysis'].values
    prof['demand'] = demand.values
    os.makedirs(WYDIR, exist_ok=True)
    prof.round(1).to_parquet(os.path.join(WYDIR, f'{ty}_wy{wy}_genmix.parquet'))
    return row


def main():
    wyears = [int(a) for a in sys.argv[1:]] or WYEARS
    t0 = time.time()
    gen = RenewableProfileGenerator(); sites = load_repd_sites(); perf = load_performance_factors()
    espeni = load_espeni_hourly(BASE_FIT_YEAR)
    base_temp = national_temperature(gen.load_cutout(str(cutout_path(BASE_FIT_YEAR))))
    dd = daily_degree_days(base_temp)
    daily = espeni.resample('D').mean().reindex(dd.index).dropna(); dd = dd.reindex(daily.index)
    params = fit_temperature_response(daily, dd['hdd'], dd['cdd']); shape = hour_of_week_shape(espeni)
    fes = filter_fes_scenario(pd.read_csv(FES_DATA_CSV), FES_SCEN)
    fleets = {ty: reference_fleet(ty) for ty in TARGET_YEARS}
    print(f'setup {time.time()-t0:.0f}s; fleets built for {TARGET_YEARS}', flush=True)

    rows = []
    for wy in wyears:
        if not cutout_path(wy).exists():
            print(f'  wy {wy}: cutout missing, skip', flush=True); continue
        tw = time.time(); cut = gen.load_cutout(str(cutout_path(wy)))
        cf = {t: apply_performance_factor(national_fleet_cf(cut, sites[t], t, generator=gen), t, perf).clip(0, 1)
              for t in TECH_BUILDING_BLOCKS}
        temp = national_temperature(cut)
        for ty in TARGET_YEARS:
            firm, minor_vre, firm_avail, storage, h2 = fleets[ty]
            m, demand, snaps = run_one(ty, cf, firm, minor_vre, firm_avail, storage, h2, params, shape, temp, fes)
            row = summarise(m, demand, snaps, ty, wy, h2)
            rows.append(row)
            print(f'  {ty} wy{wy}: VRE {row["vre_avail_twh"]:.0f} TWh, curtail {row["curtail_twh"]:.1f}, '
                  f'gas {row["gas_twh"]:.2f}, H2in {row["electrolysis_twh"]:.1f}, unserved {row["unserved_twh"]:.3f} TWh',
                  flush=True)
        pd.DataFrame(rows).to_csv(fr'{OUTDIR}\seasonal_storage_wy_summary.csv', index=False)  # checkpoint
        print(f'  wy {wy} done in {time.time()-tw:.0f}s ({len(rows)} runs, {time.time()-t0:.0f}s total)', flush=True)
    print(f'DONE {len(rows)} runs in {time.time()-t0:.0f}s -> seasonal_storage_wy_summary.csv', flush=True)


if __name__ == '__main__':
    main()
