"""
Seasonal storage-dispatch LP for PyPSA-GB.

Builds a 1-bus national copperplate version of a solved scenario (generators
aggregated by carrier using *available* VRE profiles, the storage fleet, and the
hydrogen store), then solves the WHOLE YEAR in one optimisation (full foresight).
This is tiny (~10^5 vars) so it does not hit the memory wall of the full-network
single solve, and - unlike the 24h rolling market - it lets the H2 store shift
energy seasonally (fill in windy/summer months, discharge in winter droughts).

Outputs, per scenario, to resources/analysis/:
  seasonal_storage_<scn>_soc.csv     hourly H2 SoC, electrolysis, turbine, battery SoC, unserved
  seasonal_storage_<scn>_sizing.csv  H2 store energy (GWh) -> annual unserved energy (TWh)

Run: python scripts/analysis/seasonal_storage_lp.py
"""
import sys, warnings, numpy as np, pandas as pd, pypsa
warnings.filterwarnings('ignore')

MK = r'C:\models\pypsa-gb\resources\market'
OUTDIR = r'C:\models\pypsa-gb\resources\analysis'
SCEN = {'HT30_market_w2010': '2030', 'HT40_market_w2010': '2040'}
VOLL = 6000.0
SOLVER = 'gurobi'

VRE = {'wind_offshore', 'wind_onshore', 'embedded_wind', 'solar_pv', 'embedded_solar',
       'marine', 'tidal_stream', 'shoreline_wave'}


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


def main():
    for scn, year in SCEN.items():
        print(f'=== {scn} ({year}) ===')
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
    print('DONE')


if __name__ == '__main__':
    main()
