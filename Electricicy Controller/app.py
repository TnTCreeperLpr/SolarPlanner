# app.py — Streamlit dashboard for Solar + EP Cube Planner
import streamlit as st
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import plotly.graph_objects as go

from solar_battery_planner import (
    simulate_day,
    DayInputs,
    BatteryConfig,
    HouseConfig,
    PVConfig,
)

TZ = "America/Chicago"
LAT, LON = 31.5576, -97.1290   # Waco

# ---------- helpers (no API key) ----------
def fetch_openmeteo_tomorrow_temps(lat=LAT, lon=LON, tz_str=TZ):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": tz_str,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    times, temps = data["hourly"]["time"], data["hourly"]["temperature_2m"]

    tomorrow = (datetime.now(ZoneInfo(tz_str)) + timedelta(days=1)).date()
    sel = [float(v) for t, v in zip(times, temps)
           if datetime.fromisoformat(t).date() == tomorrow]
    if len(sel) == 23: sel.append(sel[-1])
    if len(sel) > 24: sel = sel[:24]
    while len(sel) < 24: sel.append(sel[-1])
    return sel

def fetch_openmeteo_pv_kw_forecast(dc_kw=17.6, perf_ratio=0.80, lat=LAT, lon=LON, tz_str=TZ):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {"latitude": lat, "longitude": lon, "hourly": "shortwave_radiation", "timezone": tz_str}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    sw = r.json()["hourly"]["shortwave_radiation"]
    max_sw = max(1.0, max(sw))
    scale = [max(0.0, v / max_sw) for v in sw]
    return [round(dc_kw * perf_ratio * s, 2) for s in scale[:24]]

# ---------- EV window helpers ----------
def _fmt_h(h: int) -> str:
    return f"{h:02d}:00"

def _find_ev_blocks(ev_kw_by_hour: dict[int, float]):
    blocks = []
    start = None
    sum_kw = 0.0
    count = 0
    max_kw = 0.0
    for h in range(25):
        val = float(ev_kw_by_hour.get(h, 0.0)) if h < 24 else 0.0
        if val > 0 and start is None:
            start = h; sum_kw = val; count = 1; max_kw = val
        elif val > 0 and start is not None:
            sum_kw += val; count += 1; max_kw = max(max_kw, val)
        elif val <= 0 and start is not None:
            end = h
            avg_kw = sum_kw / max(1, count)
            blocks.append((start, end, round(avg_kw, 2), round(max_kw, 2)))
            start = None
    return blocks

def _blocks_to_line(label: str, blocks, target_kwh: float) -> str:
    if not blocks:
        return f"{label}: no solar-only window found for the {target_kwh:.1f} kWh request."
    parts = [f"{_fmt_h(s)}–{_fmt_h(e)} (~{avg:.1f} kW avg, up to {mx:.1f} kW)"
             for (s, e, avg, mx) in blocks]
    return f"Charge {label}: " + "; ".join(parts) + f". (Target {target_kwh:.1f} kWh)"

# ---------- UI ----------
st.set_page_config(page_title="Solar + EP Cube Planner", layout="wide")
st.title("Solar + EP Cube — Next-Day Planner")

with st.sidebar:
    st.subheader("Inputs")
    dc_kw = st.number_input("PV DC kW", value=17.6, step=0.1)
    perf_ratio = st.slider("Performance ratio", 0.6, 1.0, 0.80, 0.01)
    batt_kwh = st.number_input("Battery usable kWh", value=30.0, step=0.1)
    reserve_pct = st.slider("Battery reserve %", 5, 30, 15) / 100.0
    tons = st.slider("Heat pump size (tons)", 2.0, 6.0, 4.0, 0.5)
    ev1_need = st.number_input("Kia Niro kWh to add", value=18.0, step=1.0)
    ev2_need = st.number_input("Audi Q8 e-tron kWh to add", value=18.0, step=1.0)
    run_laundry = st.checkbox("Run Laundry", value=True)
    run_dw = st.checkbox("Run Dishwasher", value=True)
    solar_only = st.checkbox("Solar-only EV scheduling (avoid grid)", value=True)
    plan_btn = st.button("Plan Tomorrow", use_container_width=True)

if plan_btn:
    try:
        temps = fetch_openmeteo_tomorrow_temps()
        pv_forecast = fetch_openmeteo_pv_kw_forecast(dc_kw=dc_kw, perf_ratio=perf_ratio)
        tomorrow = str((datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date())

        inp = DayInputs(
            date=tomorrow,
            temp_F=temps,
            pv_kw_forecast=pv_forecast,
            battery=BatteryConfig(usable_kwh=batt_kwh, reserve_pct=reserve_pct),
            house=HouseConfig(heat_pump_tons=tons),
            pv=PVConfig(dc_kw=dc_kw, perf_ratio=perf_ratio),
            ev1_kwh_needed=ev1_need,
            ev2_kwh_needed=ev2_need,
        )

        strategy = "solar_only" if solar_only else "fixed_midday"
        df, todo, logic = simulate_day(inp, ev_strategy=strategy)

        # ---------- EV start/stop windows ----------
        ev1_by_hour = dict(zip(df["hour"], df["ev1_kw"]))
        ev2_by_hour = dict(zip(df["hour"], df["ev2_kw"]))
        ev1_blocks = _find_ev_blocks(ev1_by_hour)
        ev2_blocks = _find_ev_blocks(ev2_by_hour)
        ev1_line = _blocks_to_line("Kia Niro", ev1_blocks, ev1_need)
        ev2_line = _blocks_to_line("Audi Q8 e-tron", ev2_blocks, ev2_need)

        # ---------- Power timeline ----------
        x = df["hour"]
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=x, y=df["pv_kw"],   mode="lines+markers", name="PV (kW)"))
        fig.add_trace(go.Scatter(x=x, y=df["load_kw"], mode="lines+markers", name="Load (kW)"))
        fig.add_trace(go.Scatter(x=x, y=df["batt_kw"], mode="lines+markers", name="Battery (kW)"))  # +charge / −discharge
        fig.add_trace(go.Scatter(x=x, y=df["grid_kw"], mode="lines+markers", name="Grid (kW)"))
        fig.add_trace(go.Scatter(x=x, y=df["soc_pct"], mode="lines+markers", name="SoC (%)", yaxis="y2"))
        fig.update_layout(
            title="Power Timeline (hover to see values)",
            hovermode="x unified",
            xaxis=dict(title="Hour of day", tickmode="linear", dtick=1, range=[-0.2, 23.2]),
            yaxis=dict(title="kW"),
            yaxis2=dict(title="SoC (%)", overlaying="y", side="right", range=[0, 100]),
            legend=dict(orientation="h", yanchor="bottom", y=-0.2, x=0.0),
            margin=dict(l=40, r=40, t=60, b=40),
        )

        # ---------- Temperature chart ----------
        fig_temp = go.Figure()
        fig_temp.add_trace(go.Scatter(x=list(range(24)), y=temps, mode="lines+markers", name="Temperature (°F)"))
        fig_temp.update_layout(
            title="Temperature Forecast (°F)",
            xaxis_title="Hour of Day",
            yaxis_title="°F",
            xaxis=dict(tickmode="linear", dtick=1, range=[-0.2, 23.2]),
            hovermode="x unified",
            margin=dict(l=40, r=40, t=60, b=40),
        )

        # ---------- Display charts side by side ----------
        st.success("Plan generated.")
        col1, col2 = st.columns([2, 1])

        with col1:
            st.markdown("### Power Timeline")
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.markdown("### Temperature Forecast (°F)")
            st.plotly_chart(fig_temp, use_container_width=True)

        # ---------- Hourly table ----------
        st.markdown("### Hour-by-Hour Plan")
        st.dataframe(df, use_container_width=True)

        # ---------- To-Do ----------
        st.markdown("### To-Do")
        st.write("• " + ev1_line)
        st.write("• " + ev2_line)
        for line in todo:
            if not line.lower().startswith("charge kia") and not line.lower().startswith("charge audi"):
                st.write("• " + line)

        with st.expander("How the EV timing was calculated (logic trace)"):
            st.code("\n".join(logic), language="text")

    except Exception as e:
        st.error(f"Failed to generate plan: {e}")
else:
    st.info("Set your inputs on the left, then click **Plan Tomorrow**.")
