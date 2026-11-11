"""Component (hybrid) demand model for FES scenarios.

Literature-grounded alternative to the `gap` degree-day model. Demand is built
as the sum of three parts, each sized by the FES building blocks and shaped
separately, instead of scaling one historical curve or applying an assumed
temperature multiplier:

    demand = BASELINE  (weather-independent: today's ESPENI shape)
           + EV        (smart overnight charging profile)
           + HEAT      (heat-pump electricity = heat demand / COP(T))

The heat part follows the air-source heat-pump method of Ruhnau, Hirth &
Praktiknjo (2019, When2Heat) for the COP response and the degree-hour /
grey-box structure of Staffell, Pfenninger & Johnson (2023, Demand.ninja).
Because the heat part is sized by the FES heat-pump energy for each pathway and
year, its contribution to the peak and to day-to-day volatility grows with
electrification automatically -- no `heat_mult` assumption.

FES component energies (annual GWh per GSP), from the FES building blocks:
    heat = Dem_BB004 + Dem_BB005   (residential + I&C heat pumps)
    ev   = Dem_BB006 + Dem_BB007   (EVs)
    base = Dem_BB008               (everything else; = BB003 - BB4 - BB5 - BB6 - BB7)

This module holds the pure physics / shape functions. The pipeline wiring
(reading the blocks per GSP, regional temperatures, ESPENI base shape, FES ACS
anchor) lives in scripts/demand/load.py::generate_component_profiles.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

HEAT_BASE_C = 15.5          # heating balance-point temperature (deg C)
COP_CORRECTION = 0.85       # real-world COP de-rate (When2Heat)

# Residential space-heating WITHIN-DAY profile (hour 0..23), normalised later.
# Heat-pump heating follows occupancy/thermostat behaviour, not just outdoor
# temperature: a morning ramp (~7-8h) and an evening peak (~18-19h), with an
# overnight/mid-day setback. Shape after the BDEW/When2Heat residential heat
# day-profiles (Ruhnau, Hirth & Praktiknjo 2019), softened for the slower,
# more continuous operation of heat pumps vs gas boilers. This is applied to the
# daily heat ENERGY (which stays temperature-driven), so cold-snap volatility is
# preserved while heat lands on the real demand peaks instead of the night trough.
RESID_HEAT_HOURLY = np.array([
    0.60, 0.55, 0.50, 0.50, 0.55, 0.70,   # 00-05 overnight setback
    1.10, 1.50, 1.45, 1.15, 0.95, 0.90,   # 06-11 morning peak
    0.90, 0.90, 0.90, 0.95, 1.10, 1.35,   # 12-17 midday dip -> evening ramp
    1.50, 1.45, 1.25, 1.00, 0.80, 0.70,   # 18-23 evening peak -> wind-down
])


def cop_ashp(t_air_c):
    """Air-source heat-pump COP as a function of outdoor air temperature (deg C).

    When2Heat quadratic in the sink-source gap dT, radiator sink temperature
    T_sink = 40 - 1.0 * T_air, minimum dT of 15 K, times a 0.85 real-world
    correction. Returns a COP array/scalar, floored at 1.0.
    """
    t = np.asarray(t_air_c, dtype=float)
    t_sink = 40.0 - 1.0 * t
    dT = np.clip(t_sink - t, 15.0, None)
    cop = COP_CORRECTION * (6.08 - 0.09 * dT + 0.0005 * dT ** 2)
    return np.clip(cop, 1.0, None)


def heat_electricity_shape(t_air_c, time_index=None, base_c: float = HEAT_BASE_C,
                           occupancy=None):
    """Normalised hourly heat-pump ELECTRICITY shape from a temperature series.

    Two-stage (When2Heat) structure:
      * daily heat ENERGY is proportional to the day's heating degrees / COP, so
        cold days draw much more electricity than mild ones (COP falls as it gets
        colder) -- this carries the day-to-day and cold-snap volatility;
      * that daily energy is then spread WITHIN the day by a residential
        occupancy profile (``RESID_HEAT_HOURLY``: morning + evening peaks), not by
        temperature alone, so heat lands on the real demand peaks instead of the
        coldest (overnight) hours.

    ``t_air_c`` is the hourly temperature aligned to ``time_index``. If
    ``time_index`` is None the legacy pure-temperature shape is returned (each
    hour weighted by its own HDD/COP). Returns an array summing to 1 (or all-zero
    if there is no heating load at all).
    """
    t = np.asarray(t_air_c, dtype=float)
    hdd = np.clip(base_c - t, 0.0, None)
    elec = hdd / cop_ashp(t)                    # hourly heating-electricity demand
    total = elec.sum()
    if total <= 0:
        return np.zeros_like(elec)
    if time_index is None:
        return elec / total                      # legacy: temperature within-day

    occ = RESID_HEAT_HOURLY if occupancy is None else np.asarray(occupancy, float)
    ti = pd.DatetimeIndex(time_index)
    day_id = pd.factorize(ti.floor("D"))[0]      # integer day index
    occ_h = occ[ti.hour.to_numpy()]              # occupancy weight per timestep
    s = pd.Series(elec)
    daily_energy = s.groupby(day_id).transform("sum").to_numpy()   # today's heat energy
    occ_day_sum = pd.Series(occ_h).groupby(day_id).transform("sum").to_numpy()
    shaped = daily_energy * occ_h / np.where(occ_day_sum > 0, occ_day_sum, 1.0)
    tot = shaped.sum()
    return shaped / tot if tot > 0 else np.zeros_like(shaped)


def ev_overnight_shape(hours_of_day, floor: float = 0.12,
                       night_hour: float = 3.0, night_kappa: float = 1.8,
                       eve_hour: float = 19.0, eve_kappa: float = 2.6,
                       eve_amp: float = 0.7):
    """Normalised EV charging shape: smooth overnight (smart/ToU) + evening plug-in.

    `hours_of_day` is an array of the hour-of-day (0-23) for each timestep. The
    fleet charges as a mix of two behaviours, each a smooth von-Mises bump on the
    24 h clock (no rectangle, so no artificial step changes at window edges):

      * a dominant OVERNIGHT bump (``night_hour``~03:00) -- smart / time-of-use
        tariff charging, consistent with the FES "EV charging is smart" ACS
        assumption; it fills the overnight valley;
      * a secondary EVENING bump (``eve_hour``~19:00, scaled by ``eve_amp``) --
        the unmanaged / arrive-home share, which keeps EV load partly on the real
        evening demand peak rather than wholly flattening the day.

    ``eve_amp`` is effectively the unmanaged:smart ratio; ``floor`` keeps some
    daytime (workplace/opportunity) charging. Defaults are calibrated so the
    aggregate within-day variability and hour-to-hour ramps stay consistent with
    observed GB demand. Returns an array summing to 1.
    """
    h = np.asarray(hours_of_day, dtype=float)
    def _vm(mu, k):
        return np.exp(k * np.cos(2.0 * np.pi * (h - mu) / 24.0))
    w = floor + _vm(night_hour, night_kappa) + eve_amp * _vm(eve_hour, eve_kappa)
    return w / w.sum()


def _mean_preserving_stretch(x, alpha):
    """Stretch deviations about the mean by alpha, preserving the mean exactly."""
    m = x.mean()
    return m + alpha * (x - m)


def anchor_baseline_to_peak(base_hourly, other_hourly, target_peak_mw,
                            max_alpha: float = 6.0, tol_mw: float = 5.0):
    """Lift the BASELINE peakiness so the national peak hits the FES ACS target.

    Only the weather-independent baseline is stretched (mean-preserving, so its
    annual energy is unchanged); the EV and heat parts are left untouched, so the
    weather-driven volatility is not inflated. Returns the stretched baseline.

    base_hourly   : national baseline demand, MW per hour (1-D)
    other_hourly  : national EV + heat demand, MW per hour (1-D)
    target_peak_mw: FES ACS national peak (MW)

    If the un-stretched peak already exceeds the target, the baseline is returned
    unchanged (we never flatten below the physical peak).
    """
    base = np.asarray(base_hourly, dtype=float)
    other = np.asarray(other_hourly, dtype=float)
    peak_now = (base + other).max()
    if target_peak_mw is None or peak_now >= target_peak_mw:
        return base
    lo, hi = 1.0, max_alpha
    # bisection on alpha: total peak is monotonically increasing in alpha
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        peak = (_mean_preserving_stretch(base, mid) + other).max()
        if abs(peak - target_peak_mw) <= tol_mw:
            lo = hi = mid
            break
        if peak < target_peak_mw:
            lo = mid
        else:
            hi = mid
    alpha = 0.5 * (lo + hi)
    stretched = np.clip(_mean_preserving_stretch(base, alpha), 0.0, None)
    # restore baseline energy exactly after any clip
    s = base.sum() / stretched.sum() if stretched.sum() > 0 else 1.0
    return stretched * s
