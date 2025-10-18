"""
solar_battery_planner.py — Next-day solar + EP Cube planner (M1+)
- Forecast-driven HVAC from temps + your setpoints
- Optional explicit HVAC profile
- Solar-only EV scheduling (or fixed-midday)
- Returns hourly table, To-Do list, and a logic trace of EV timing
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
import math
import pandas as pd

# -----------------------------
# Configuration dataclasses
# -----------------------------
@dataclass
class PVConfig:
    dc_kw: float = 17.6
    perf_ratio: float = 0.80           # overall system losses
    peak_hour_local: int = 13          # ~1pm typical

@dataclass
class BatteryConfig:
    usable_kwh: float = 30.0
    reserve_pct: float = 0.15          # 10–15% on clear days
    max_kw_charge: float = 15.0
    max_kw_discharge: float = 15.0
    round_trip_eff: float = 0.92

@dataclass
class RatePlan:
    kind: str = "flat"                 # flat | tou | free_nights | buyback_1to1
    day_rate: float = 0.166
    buyback_rate: float = 0.0

@dataclass
class EVConfig:
    name: str
    max_kw: float
    default_kw: float                   # “polite” hourly cap, e.g. 24A ~5.7 kW

@dataclass
class ComfortSetpoints:
    day: float = 72.0                   # 10a–5p pre-cool
    evening: float = 75.0               # 5p–10p
    night: float = 78.0                 # 10p–6a

@dataclass
class Location:
    lat: float = 31.5483                # Waco, TX
    lon: float = -97.1467
    tz: str = "America/Chicago"

@dataclass
class HouseConfig:
    base_kw_day: float = 1.0
    base_kw_night: float = 0.8
    pool_kw: float = 1.8
    pool_start: int = 8
    pool_end: int = 17                   # 5pm exclusive
    hvac_kw_profile: Optional[List[float]] = None
    heat_pump_tons: float = 4.0

@dataclass
class AppliancePlan:
    run_laundry: bool = True
    run_dishwasher: bool = True
    laundry_kwh: float = 1.2
    laundry_hours: int = 1
    dishwasher_kwh: float = 1.4
    dishwasher_hours: int = 2

@dataclass
class DayInputs:
    date: str
    location: Location = field(default_factory=Location)
    setpoints: ComfortSetpoints = field(default_factory=ComfortSetpoints)
    temp_F: Optional[List[float]] = None           # 24 values (°F)
    pv: PVConfig = field(default_factory=PVConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    rates: RatePlan = field(default_factory=RatePlan)
    house: HouseConfig = field(default_factory=HouseConfig)
    ev1: EVConfig = field(default_factory=lambda: EVConfig("Kia Niro", max_kw=7.2, default_kw=5.7))
    ev2: EVConfig = field(default_factory=lambda: EVConfig("Audi Q8 e-tron", max_kw=9.6, default_kw=5.7))
    ev1_kwh_needed: float = 0.0
    ev2_kwh_needed: float = 0.0
    pv_kw_forecast: Optional[List[float]] = None   # 24 values (kW)

# -----------------------------
# Helpers: PV, HVAC, appliances, EV logic
# -----------------------------
def clear_sky_curve(pv: PVConfig) -> List[float]:
    """Smooth cosine-bell PV curve 7:00–19:00 with peak at peak_hour_local."""
    peak_kw = pv.dc_kw * pv.perf_ratio
    arr = [0.0] * 24
    for h in range(7, 19):
        x = (h - pv.peak_hour_local) / 6.0 * math.pi
        y = max(0.0, math.cos(x))
        arr[h] = peak_kw * y
    return arr

def hvac_kw_from_forecast(temps_F: List[float], setpoints: ComfortSetpoints, tons: float) -> List[float]:
    """
    Simple cooling model:
      full_kw ≈ 1.3 * tons
      duty = clamp((T_out - setpoint)/15, 0..1)
    """
    full_kw = max(2.0, 1.3 * tons)
    hp = []
    for h, tF in enumerate(temps_F):
        if 10 <= h < 17:
            sp = setpoints.day
        elif 17 <= h < 22:
            sp = setpoints.evening
        else:
            sp = setpoints.night
        duty = max(0.0, min(1.0, (tF - sp) / 15.0))
        hp.append(full_kw * duty)
    return hp

def build_house_load(house: HouseConfig, temps_F: Optional[List[float]], setpoints: ComfortSetpoints) -> List[float]:
    """Baseline + pool + HVAC (forecast-driven if temps provided, else heuristic)."""
    hvac_profile: Optional[List[float]] = None
    if house.hvac_kw_profile is not None:
        hvac_profile = house.hvac_kw_profile
    elif temps_F is not None and len(temps_F) == 24:
        hvac_profile = hvac_kw_from_forecast(temps_F, setpoints, house.heat_pump_tons)

    load = []
    for h in range(24):
        base = house.base_kw_day if 8 <= h < 22 else house.base_kw_night
        pool = house.pool_kw if house.pool_start <= h < house.pool_end else 0.0
        if hvac_profile is not None:
            hvac = hvac_profile[h]
        else:
            # Fallback heuristic
            if 11 <= h < 17:   hvac = 1.5
            elif 17 <= h < 22: hvac = 2.0
            elif 0 <= h < 6:   hvac = 0.8
            else:              hvac = 1.0
        load.append(base + pool + hvac)
    return load

def allocate_appliances(appl: AppliancePlan) -> Dict[str, List[int]]:
    """Choose daytime hour slots for laundry & dishwasher (favor 11–15)."""
    slots = {"laundry": [], "dishwasher": []}
    candidates = list(range(11, 16)) + [10, 16]
    if appl.run_laundry:
        for h in candidates:
            if len(slots["laundry"]) < appl.laundry_hours:
                slots["laundry"].append(h)
    if appl.run_dishwasher:
        for h in candidates:
            if len(slots["dishwasher"]) < appl.dishwasher_hours:
                slots["dishwasher"].append(h)
    return slots

def hourly_appliance_kw(hours: List[int], total_kwh: float) -> Dict[int, float]:
    if not hours: return {}
    per_hour = total_kwh / len(hours)
    return {h: per_hour for h in hours}

def plan_ev_windows(ev: EVConfig, kwh_needed: float) -> List[Tuple[int, float]]:
    """Original fixed 10–18 plan (may import grid). Returns (hour, kW)."""
    if kwh_needed <= 0: return []
    hours = list(range(10, 19))
    plan = []
    remaining = kwh_needed
    for h in hours:
        if remaining <= 0: break
        kw = min(ev.default_kw, ev.max_kw)
        deliver = min(kw, remaining)
        plan.append((h, deliver))
        remaining -= deliver
    if remaining > 0:
        for h in hours:
            if remaining <= 0: break
            kw = ev.max_kw
            deliver = min(kw, remaining)
            plan.append((h, deliver))
            remaining -= deliver
    return plan

def plan_ev_from_surplus(pv_curve, base_curve, ev1_need, ev2_need,
                         ev1_max_kw, ev2_max_kw) -> Tuple[Dict[int, float], Dict[int, float], List[str]]:
    """
    Greedy allocation of EV charging into hours with PV > base load (surplus).
    Returns dicts {hour: kW} for EV1/EV2 and a human-readable trace.
    """
    trace = []
    surplus = [max(0.0, pv - base) for pv, base in zip(pv_curve, base_curve)]
    hours = list(range(24))
    # Rank: larger surplus first; slight bias to midday (closer to 12)
    hours.sort(key=lambda h: (surplus[h], 12 - abs(h-12)), reverse=True)

    ev1_kw, ev2_kw = {}, {}
    r1, r2 = ev1_need, ev2_need

    trace.append("EV strategy: solar_only (allocate from PV surplus only).")
    trace.append(f"Surplus by hour (kW): {[round(x,2) for x in surplus]}")
    trace.append(f"Ranked hours: {hours}")

    for h in hours:
        if r1 <= 0 and r2 <= 0:
            break
        avail = surplus[h]
        if avail <= 0:
            continue

        give1 = min(ev1_max_kw, r1, avail)
        if give1 > 0:
            ev1_kw[h] = round(give1, 2)
            r1 -= give1
            avail -= give1

        give2 = min(ev2_max_kw, r2, avail)
        if give2 > 0:
            ev2_kw[h] = round(give2, 2)
            r2 -= give2
            avail -= give2

        trace.append(f"h{h:02d}: surplus={surplus[h]:.2f} → EV1={ev1_kw.get(h,0):.2f} kW, EV2={ev2_kw.get(h,0):.2f} kW")

    if r1 > 0 or r2 > 0:
        trace.append(f"Unfilled demand: EV1 {r1:.1f} kWh, EV2 {r2:.1f} kWh (insufficient surplus).")

    return ev1_kw, ev2_kw, trace

# -----------------------------
# Core simulation
# -----------------------------
def simulate_day(inp: DayInputs,
                 appl: AppliancePlan = AppliancePlan(),
                 ev_strategy: str = "fixed_midday") -> Tuple[pd.DataFrame, List[str], List[str]]:
    """Return (df, todo, logic)."""
    pv_curve = inp.pv_kw_forecast if inp.pv_kw_forecast else clear_sky_curve(inp.pv)
    house_curve = build_house_load(inp.house, inp.temp_F, inp.setpoints)

    # Appliances (kWh spread as flat kW over chosen hours)
    slots = allocate_appliances(appl)
    appl_kw = [0.0] * 24
    for name, hours in slots.items():
        if name == "laundry":
            for h, kw in hourly_appliance_kw(hours, appl.laundry_kwh).items():
                appl_kw[h] += kw
        elif name == "dishwasher":
            for h, kw in hourly_appliance_kw(hours, appl.dishwasher_kwh).items():
                appl_kw[h] += kw

    # Base (no EV) for surplus math
    base_curve = [hc + ak for hc, ak in zip(house_curve, appl_kw)]

    logic: List[str] = []
    if ev_strategy == "solar_only":
        ev1_kw_dict, ev2_kw_dict, trace = plan_ev_from_surplus(
            pv_curve, base_curve,
            inp.ev1_kwh_needed, inp.ev2_kwh_needed,
            inp.ev1.default_kw, inp.ev2.default_kw
        )
        logic += trace
        ev1_kw = ev1_kw_dict
        ev2_kw = ev2_kw_dict
    else:
        logic.append("EV strategy: fixed_midday (10:00–18:00). May import grid.")
        ev1_sched = plan_ev_windows(inp.ev1, inp.ev1_kwh_needed)
        ev2_sched = plan_ev_windows(inp.ev2, inp.ev2_kwh_needed)
        ev1_kw = {h: kw for h, kw in ev1_sched}
        ev2_kw = {h: kw for h, kw in ev2_sched}

    # Simulation state
    soc_kwh = inp.battery.usable_kwh * (1.0 - inp.battery.reserve_pct)  # start near full
    soc_max = inp.battery.usable_kwh
    soc_min = inp.battery.usable_kwh * inp.battery.reserve_pct

    rows = []
    todo: List[str] = []

    for h in range(24):
        pv = pv_curve[h]
        base_load = base_curve[h]                  # house + appliances
        ev1 = ev1_kw.get(h, 0.0)
        ev2 = ev2_kw.get(h, 0.0)

        load = base_load + ev1 + ev2
        batt_kw = 0.0
        grid_kw = 0.0

        surplus = pv - load

        if surplus >= 0:
            # charge battery with surplus up to limits/capacity
            charge_cap = min(inp.battery.max_kw_charge, surplus)
            energy_into = charge_cap * inp.battery.round_trip_eff
            available_capacity = soc_max - soc_kwh
            if energy_into > available_capacity:
                energy_into = max(0.0, available_capacity)
                charge_cap = energy_into / max(inp.battery.round_trip_eff, 1e-6)
            soc_kwh += energy_into
            batt_kw = charge_cap
            grid_kw = surplus - charge_cap  # export if > 0
        else:
            # discharge battery down to reserve, then grid
            deficit = -surplus
            discharge_cap = min(inp.battery.max_kw_discharge, deficit)
            needed_from_batt = discharge_cap / max(inp.battery.round_trip_eff, 1e-6)
            available_from_batt = max(0.0, soc_kwh - soc_min)
            energy_from_batt = min(needed_from_batt, available_from_batt)
            delivered = energy_from_batt * inp.battery.round_trip_eff
            soc_kwh -= energy_from_batt
            batt_kw = -delivered
            remaining_deficit = deficit - delivered
            grid_kw = max(0.0, remaining_deficit)

        rows.append({
            "hour": h,
            "pv_kw": round(pv, 2),
            "load_kw": round(load, 2),
            "batt_kw": round(batt_kw, 2),
            "grid_kw": round(grid_kw, 2),
            "soc_pct": round(100.0 * soc_kwh / soc_max, 1),
            "ev1_kw": round(ev1, 2),
            "ev2_kw": round(ev2, 2),
            "appliances_kw": round(appl_kw[h], 2)
        })

    df = pd.DataFrame(rows)

    # To-Do summary
    if any(ev1_kw.values()):
        todo.append("Charge Kia Niro during allocated solar-surplus hours.")
    if any(ev2_kw.values()):
        todo.append("Charge Audi Q8 e-tron during allocated solar-surplus hours.")
    if slots["laundry"]:
        todo.append(f"Run laundry at: {', '.join(str(h)+':00' for h in slots['laundry'])}.")
    if slots["dishwasher"]:
        todo.append(f"Run dishwasher at: {', '.join(str(h)+':00' for h in slots['dishwasher'])}.")
    todo.append("Pre-cool to ~72°F 10:00–17:00; 75°F evening; 77–78°F overnight.")

    return df, todo, logic
