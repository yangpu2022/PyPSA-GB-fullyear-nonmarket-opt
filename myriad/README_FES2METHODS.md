# FES two-method, two-stage market campaign (2030/2040/2050, 41 weather years)

**What this run is.** 2 demand methods × 4 FES2025 pathways × {2030, 2040, 2050} ×
weather years **1985–2025** (41) = **984 full-year, Reduced-network, two-stage
(wholesale + Balancing Mechanism) solves**. Target per scenario:
`resources/market/{scenario}_balancing.nc`.

**The two demand methods (sensitivity bound).**
- `esp` — `demand_timeseries: "espeni"`: the native PyPSA-GB method, the historical
  demand shape scaled to the FES ED1 annual total per GSP (weather-coherent via
  `demand_year` = the weather year). Lower bound on peakiness.
- `cmp` — `demand_timeseries: "component"`: baseline (ESPENI shape × `Dem_BB008`) +
  smart-overnight EV (`Dem_BB006/007`) + heat-pump electricity = heat ÷ COP(T)
  (`Dem_BB004/005`), per GSP on its own temperature. Weather-sensitivity grows with
  the FES heat-pump stock (Staffell/Pfenninger Demand.ninja + Ruhnau When2Heat). The
  ACS peak-anchor is **off** by default (`component_acs_anchor`), so peaks stay
  weather-driven across the 41 years.

Scenario id: `{HT|EE|HE|FB}{30|40|50}_{esp|cmp}_w{YYYY}`, e.g. `HT30_cmp_w2010`.
The matrix lives in `config/scenarios.d/fes_2methods_campaign.yaml` (auto-merged by
`config/config_loader.py`); regenerate with
`python scripts/FES/generate_fes_2methods_campaign.py`.

---

## 0. Prerequisites on Myriad (same as the gap run)
- Repo clone + conda env at `~/Assume_GB/pypsa-gb` / `~/Assume_GB/pypsa-gb-env`.
- All 41 ERA5 cutouts `resources/atlite/cutouts/uk-1985.nc … uk-2025.nc`
  (the `component` method uses the weather-year cutout; it does **not** need a
  base-fit cutout).

## 1. Get the branch onto Myriad
```bash
ssh ucbqnge@myriad.rc.ucl.ac.uk
cd ~/Assume_GB/pypsa-gb
git fetch origin && git checkout gap-demand-dispatch && git pull
```

## 2. Pilot FIRST (7 scenarios: both methods, all three years, cold+mild weather)
```bash
cd ~/Assume_GB/pypsa-gb
bash -l myriad/submit_fes2methods.sh pilot
# confirm a cmp and an esp target exist before scaling out:
ls -la resources/market/HT30_cmp_w2010_balancing.nc resources/market/HT30_esp_w2010_balancing.nc
```

## 3. Scale out (target ≈ 12 h at -tc 80)
```bash
MAXCONC=80 bash -l myriad/submit_fes2methods.sh esp   # tasks   1-492
MAXCONC=80 bash -l myriad/submit_fes2methods.sh cmp   # tasks 493-984
# or everything at once:  MAXCONC=80 bash -l myriad/submit_fes2methods.sh all
```
`-tc 80` throttles to ≤80 concurrent array tasks. The ~12 h target assumes ~80 tasks
run at once at ~45 min each (984 × 0.75 / 80 ≈ 9 h); if the scheduler grants fewer it
runs longer. Raise with `MAXCONC=100`.

## 4. Monitor
```bash
qstat
ls resources/market/*_balancing.nc | wc -l     # progress (target: 984)
tail -f logs/fes2m.o*
```

## 5. Pull results back (in YOUR VPN PowerShell, not via Claude)
Adapt `myriad/pull_gap_results.ps1` to copy `resources/market/*_balancing.nc`.

---

## Tuning / caveats
- **Resources** (`run_fes2methods.qsub`): `h_rt=48:00:00`, `mem=24G`, `smp 4`. The BM
  stage is nodal and heavier than the gap dispatch; if tasks OOM, raise `mem`.
- **Verified**: the demand methods are validated locally (energy = FES ED1 exactly,
  regional-peak correlation 0.95, realistic daily volatility) and the two-stage DAG
  resolves for a `cmp` market scenario. The **full solve** has not been run on Myriad —
  run the `pilot` stage and confirm one `cmp` and one `esp` `*_balancing.nc` first.
- **Falling Behind** has no regional peak in the ETYS workbook; its component anchor,
  when enabled, uses the national ACS only. With the anchor off (default) this is moot.
