# Gap-demand full-year dispatch campaign (2030 & 2040, all weather years)

**What this run is.** FES 2025 × 4 pathways (Holistic Transition, Electric
Engagement, Hydrogen Evolution, Falling Behind) × {2030, 2040} × weather years
**1985–2025** (41 years) = **328 full-year Reduced-network economic-dispatch
solves**, market off. Target per scenario: `resources/network/{scenario}_solved.nc`.

**How demand is adjusted by the gap analysis.** These scenarios set
`demand_timeseries: "gap"`. The demand *temporal shape* comes from the gap
weather-demand model (`scripts/gap/demand_weather_model.py::project_demand`):
a temperature response is fitted on a historical base year (ESPENI demand vs
heating/cooling degree-days, `gap.base_fit_demand_year`, default **2019**), then
re-evaluated against each scenario's **weather year** temperature, with the
electrified-heating slope scaled up for the future (`gap_case`, default
**central**). So cold weather years raise winter/peak demand. The annual demand
**level** per GSP is still taken from FES ED1 totals — the gap model only drives
the shape, so there is no double counting of the FES level.

Scenario naming: `{HT|EE|HE|FB}{30|40}_gap_w{YYYY}`, e.g. `HT30_gap_w2010`.

---

## 0. Prerequisites on Myriad (one-off)
- Repo cloned at `~/Assume_GB/pypsa-gb` and conda env at `~/Assume_GB/pypsa-gb-env`
  (built by `assume-gb/myriad/setup_pypsa_gb.sh`).
- All 41 ERA5 cutouts present at
  `~/Assume_GB/pypsa-gb/resources/atlite/cutouts/uk-1985.nc … uk-2025.nc`
  (uploaded by `assume-gb/myriad/upload_cutouts_to_myriad.ps1`). The gap model
  additionally needs the **base-fit-year cutout `uk-2019.nc`** — it is in that set.

## 1. Apply this changeset to the authoritative repo (`nmopt`)
From a machine with a **working** clone of
`PyPSA-GB-fullyear-nonmarket-opt` on branch `worktree-run-2030-2040`:

```bash
# Option A — apply the patch
git checkout -b gap-demand-dispatch worktree-run-2030-2040
git apply --whitespace=nowarn /path/to/gap-demand-dispatch.patch
git add -A && git commit -m "Gap-demand full-year dispatch: 2030/2040 x 4 pathways x weather 1985-2025"

# Option B — pull straight from the bundle
git fetch /path/to/gap-demand-dispatch.bundle gap-demand-dispatch:gap-demand-dispatch
git checkout gap-demand-dispatch

git push nmopt gap-demand-dispatch
```

## 2. Get the branch onto Myriad
```bash
ssh ucbqnge@myriad.rc.ucl.ac.uk
cd ~/Assume_GB/pypsa-gb
git fetch origin && git checkout gap-demand-dispatch && git pull
```

## 3. Submit — staged, one pathway at a time
```bash
cd ~/Assume_GB/pypsa-gb
bash -l myriad/submit_gap_dispatch.sh pilot   # OPTIONAL: HT 2030/2040 x {2010,2015,2020}
bash -l myriad/submit_gap_dispatch.sh HT      # tasks 1-82   (check a few *_solved.nc)
bash -l myriad/submit_gap_dispatch.sh EE      # tasks 83-164
bash -l myriad/submit_gap_dispatch.sh HE      # tasks 165-246
bash -l myriad/submit_gap_dispatch.sh FB      # tasks 247-328
# or everything at once:  bash -l myriad/submit_gap_dispatch.sh all
```
`-tc 20` throttles to ≤20 concurrent array tasks (override with `MAXCONC=…`).
Each task runs `snakemake resources/network/<scenario>_solved.nc --config scenario=<scenario>`.

## 4. Monitor
```bash
qstat                       # queue
ls resources/network/*_solved.nc | wc -l     # progress (target: 328)
tail -f logs/gap_disp.o*    # a running task's log
```

## 5. Pull results back (in YOUR VPN PowerShell, not via Claude)
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  "C:\Users\ucbqnge\Assume_GB\assume-gb\myriad\pull_gap_results.ps1"
```

---

## Tuning / caveats
- **Resources** (`run_gap_dispatch.qsub`): `h_rt=48:00:00`, `mem=16G`, `smp 4`.
  A full year is 8760 hourly timesteps on the 32-bus Reduced network with
  component + renewable aggregation. If tasks hit the wall or OOM, raise
  `mem`/`h_rt` or lower the solve scope.
- **Electrification case**: `gap_case: central` on every scenario. Switch a
  scenario (or set globally under `gap.case` in `config/defaults.yaml`) to
  `low`/`high` to widen the heating sensitivity.
- **`demand_year`** is set to the weather year for readability but is **unused**
  on the gap path (the fit base year is `gap.base_fit_demand_year`).
- **Verification**: this changeset compiles and the 328 scenarios parse, but it
  has **not** been run end-to-end (no Myriad access from where it was built).
  Run the `pilot` stage first and confirm one `*_solved.nc` before scaling out.
