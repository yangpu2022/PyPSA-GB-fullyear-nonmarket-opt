"""Weather-dependent electrified demand for future years (gap-analysis layer).

Ported onto the full-year dispatch branch so ``build_base_demand`` can use the
gap demand projection as the temporal shape for ``demand_timeseries: "gap"``.

The degree-hour / hour-of-week / OLS-fit logic is unchanged from the offline
gap pipeline; only the Snakemake ``main()`` entry point has been dropped (the
dispatch pipeline calls ``project_demand`` directly from scripts/demand/load.py).
"""
from __future__ import annotations
import numpy as np
import pandas as pd

HEAT_BASE_C = 15.5   # matches build_heat_profiles.HEATING_BASE_TEMP
COOL_BASE_C = 22.0   # GB cooling threshold


def national_temperature(cutout) -> pd.Series:
    """National spatial-mean 2m temperature (Celsius) as an hourly Series."""
    temp = cutout.data["temperature"]            # (time, y, x), Kelvin or C
    nat = temp.mean(dim=["y", "x"]).to_series()
    if nat.mean() > 200:                          # Kelvin -> Celsius
        nat = nat - 273.15
    return nat


def degree_hours(temp_c: pd.Series, heat_base: float = HEAT_BASE_C,
                 cool_base: float = COOL_BASE_C) -> pd.DataFrame:
    """Heating and cooling degree-hours per timestep from Celsius temperature."""
    hdh = np.maximum(0.0, heat_base - temp_c)
    cdh = np.maximum(0.0, temp_c - cool_base)
    return pd.DataFrame({"hdh": hdh, "cdh": cdh})


def daily_degree_days(temp_c: pd.Series, heat_base: float = HEAT_BASE_C,
                      cool_base: float = COOL_BASE_C) -> pd.DataFrame:
    """Daily heating/cooling degree-days from an hourly Celsius temperature series."""
    daily = temp_c.resample("D").mean()
    hdd = np.maximum(0.0, heat_base - daily)
    cdd = np.maximum(0.0, daily - cool_base)
    return pd.DataFrame({"hdd": hdd, "cdd": cdd})


def fit_temperature_response(daily_demand_mw: pd.Series, daily_hdd: pd.Series,
                             daily_cdd: pd.Series) -> dict:
    """OLS fit of daily mean demand on heating and cooling degree-days.

    demand = base_mw + heat_slope*HDD + cool_slope*CDD
    """
    X = np.column_stack([
        np.ones(len(daily_demand_mw)),
        np.asarray(daily_hdd, dtype=float),
        np.asarray(daily_cdd, dtype=float),
    ])
    y = np.asarray(daily_demand_mw, dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {"base_mw": float(coef[0]), "heat_slope": float(coef[1]), "cool_slope": float(coef[2])}


ELECTRIFICATION_CASES = {
    "low":     {"total_mult": 0.92, "heat_mult": 2.0},
    "central": {"total_mult": 1.00, "heat_mult": 3.0},
    "high":    {"total_mult": 1.10, "heat_mult": 4.0},
}


def hour_of_week_shape(hourly_demand_mw: pd.Series) -> pd.Series:
    """168-length normalized hour-of-week base shape (mean 1.0)."""
    idx = pd.DatetimeIndex(hourly_demand_mw.index)
    how = idx.dayofweek * 24 + idx.hour
    shape = hourly_demand_mw.groupby(how).mean()
    shape = shape.reindex(range(168)).interpolate().bfill().ffill()
    return shape / shape.mean()


def project_demand(temp_c: pd.Series, params: dict, base_shape: pd.Series,
                   annual_total_gwh: float, heat_mult: float, total_mult: float) -> pd.Series:
    """Hourly electrified demand (MW) for one weather year and case.

    Weather-insensitive base follows the hour-of-week shape; the
    temperature-sensitive heating/cooling increment uses the fitted slopes,
    with heating scaled by `heat_mult` for future electrification. The whole
    series is then rescaled so its annual energy equals
    annual_total_gwh*total_mult (the weather year sets shape/extremes, FES sets
    level).
    """
    idx = pd.DatetimeIndex(temp_c.index)
    how = idx.dayofweek * 24 + idx.hour
    base = params["base_mw"] * base_shape.reindex(how).to_numpy()
    dh = degree_hours(temp_c)
    weather = heat_mult * params["heat_slope"] * dh["hdh"].to_numpy() \
        + params["cool_slope"] * dh["cdh"].to_numpy()
    raw = pd.Series(base + weather, index=idx).clip(lower=0.0)
    target_mwh = annual_total_gwh * total_mult * 1000.0
    return raw * (target_mwh / raw.sum())
