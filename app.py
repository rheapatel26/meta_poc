# app.py
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import subprocess, time, re, json, os
from datetime import datetime

# ─── ADB helpers ──────────────────────────────────────────────
def adb(cmd):
    result = subprocess.run(
        f"adb shell {cmd}", shell=True,
        capture_output=True, text=True, timeout=5
    )
    return result.stdout

def get_battery():
    raw = adb("dumpsys battery")
    level = re.search(r"level: (\d+)", raw)
    temp  = re.search(r"temperature: (\d+)", raw)
    volt  = re.search(r"voltage: (\d+)", raw)
    return {
        "battery_pct":  int(level.group(1)) if level else None,
        "battery_temp": int(temp.group(1)) / 10 if temp else None,  # in °C
        "battery_volt": int(volt.group(1)) / 1000 if volt else None, # in V
    }

def get_memory(pkg):
    raw = adb(f"dumpsys meminfo {pkg}")
    pss = re.search(r"TOTAL PSS:\s+(\d+)", raw)
    rss = re.search(r"TOTAL RSS:\s+(\d+)", raw)
    return {
        "mem_pss_mb": int(pss.group(1)) / 1024 if pss else None,
        "mem_rss_mb": int(rss.group(1)) / 1024 if rss else None,
    }

def get_cpu(pkg):
    raw = adb("dumpsys cpuinfo")
    match = re.search(rf"([\d.]+)% .+{re.escape(pkg)}", raw)
    return {"cpu_pct": float(match.group(1)) if match else 0.0}

def get_fps(pkg):
    raw = adb(f"dumpsys gfxinfo {pkg}")
    janky = re.search(r"Janky frames: (\d+)", raw)
    total = re.search(r"Total frames rendered: (\d+)", raw)
    return {
        "janky_frames": int(janky.group(1)) if janky else None,
        "total_frames": int(total.group(1)) if total else None,
    }

def get_thermals():
    raw = adb("cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null")
    temps = [int(t)/1000 for t in raw.split() if t.isdigit() and int(t) < 200000]
    return {"max_thermal_c": max(temps) if temps else None}

def get_foreground_pkg():
    raw = adb("dumpsys activity | grep mResumedActivity")
    match = re.search(r"u0 ([\w.]+)/", raw)
    return match.group(1) if match else None

def collect_snapshot(pkg, ts):
    row = {"timestamp": ts}
    row.update(get_battery())
    row.update(get_memory(pkg))
    row.update(get_cpu(pkg))
    row.update(get_fps(pkg))
    row.update(get_thermals())
    return row

# ─── Streamlit UI ─────────────────────────────────────────────
st.set_page_config(page_title="Android Game Profiler", layout="wide")
st.title("🎮 Android Game Profiler")
st.caption("LnT × Meta Internship Tool | Capture 60s of performance data via ADB")

# Session state init
for key in ["running", "data", "pkg", "log_path"]:
    if key not in st.session_state:
        st.session_state[key] = None if key != "data" else []

# ─── Sidebar controls ─────────────────────────────────────────
with st.sidebar:
    st.header("Configuration")
    auto_detect = st.toggle("Auto-detect game", value=True)
    
    if auto_detect:
        if st.button("🔍 Detect foreground app"):
            detected = get_foreground_pkg()
            st.session_state.pkg = detected
            st.success(f"Detected: {detected}" if detected else "Nothing detected")
        pkg = st.session_state.pkg or ""
    else:
        pkg = st.text_input("Package name", placeholder="com.supercell.clashofclans")
        st.session_state.pkg = pkg

    duration = st.slider("Capture duration (sec)", 10, 120, 60)
    poll_interval = st.selectbox("Poll interval", [0.5, 1, 2], index=1)
    st.divider()
    st.markdown("**ADB status**")
    if st.button("Check ADB connection"):
        out = subprocess.run("adb devices", shell=True, capture_output=True, text=True)
        st.code(out.stdout)

# ─── Main controls ────────────────────────────────────────────
col1, col2, col3 = st.columns([2,2,4])
with col1:
    start_btn = st.button("▶ Start Tracking", type="primary",
                          disabled=not pkg or st.session_state.running)
with col2:
    stop_btn  = st.button("⏹ Stop", disabled=not st.session_state.running)

if start_btn and pkg:
    st.session_state.running = True
    st.session_state.data = []
    progress = st.progress(0, text="Starting capture...")
    status   = st.empty()
    
    for i in range(int(duration / poll_interval)):
        if stop_btn:
            break
        snap = collect_snapshot(pkg, datetime.now().isoformat())
        st.session_state.data.append(snap)
        pct = (i + 1) / int(duration / poll_interval)
        progress.progress(pct, text=f"Capturing... {i+1}/{int(duration/poll_interval)} samples")
        time.sleep(poll_interval)
    
    st.session_state.running = False
    # Save log
    df = pd.DataFrame(st.session_state.data)
    fname = f"game_profile_{pkg}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    df.to_csv(fname, index=False)
    st.session_state.log_path = fname
    status.success(f"Capture complete! Saved to {fname}")

# ─── Results dashboard ────────────────────────────────────────
if st.session_state.data:
    df = pd.DataFrame(st.session_state.data)
    df["time_s"] = range(len(df))
    
    st.divider()
    st.subheader("📊 Session Summary")
    
    # KPI cards
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Avg Battery %",    f"{df['battery_pct'].mean():.1f}%",
              f"{df['battery_pct'].iloc[-1] - df['battery_pct'].iloc[0]:+.0f}% drain")
    m2.metric("Max Temp (°C)",    f"{df['battery_temp'].max():.1f}°",
              f"avg {df['battery_temp'].mean():.1f}°")
    m3.metric("Peak CPU %",       f"{df['cpu_pct'].max():.1f}%",
              f"avg {df['cpu_pct'].mean():.1f}%")
    m4.metric("Peak RAM (MB)",    f"{df['mem_pss_mb'].max():.0f} MB",
              f"avg {df['mem_pss_mb'].mean():.0f} MB")
    m5.metric("Max Thermal (°C)", f"{df['max_thermal_c'].max():.1f}°",
              f"avg {df['max_thermal_c'].mean():.1f}°")
    
    # Charts
    tab1, tab2, tab3 = st.tabs(["🔋 Battery & Temp", "⚡ CPU & RAM", "🎞 FPS & Frames"])
    
    with tab1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["time_s"], y=df["battery_pct"],
                                  name="Battery %", line=dict(color="#2ecc71", width=2)))
        fig.add_trace(go.Scatter(x=df["time_s"], y=df["battery_temp"],
                                  name="Temp (°C)", line=dict(color="#e74c3c", width=2),
                                  yaxis="y2"))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", title="Temp °C"),
                          xaxis_title="Time (s)", yaxis_title="Battery %",
                          legend=dict(x=0, y=1), height=360)
        st.plotly_chart(fig, use_container_width=True)
    
    with tab2:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df["time_s"], y=df["cpu_pct"],
                                   name="CPU %", fill="tozeroy", line=dict(color="#3498db")))
        fig2.add_trace(go.Scatter(x=df["time_s"], y=df["mem_pss_mb"],
                                   name="RAM PSS (MB)", line=dict(color="#9b59b6"),
                                   yaxis="y2"))
        fig2.update_layout(yaxis2=dict(overlaying="y", side="right", title="MB"),
                           xaxis_title="Time (s)", yaxis_title="CPU %", height=360)
        st.plotly_chart(fig2, use_container_width=True)
    
    with tab3:
        if "janky_frames" in df.columns:
            fig3 = px.bar(df, x="time_s", y="janky_frames",
                          color="janky_frames", color_continuous_scale="Reds",
                          labels={"time_s": "Time (s)", "janky_frames": "Janky Frames"})
            st.plotly_chart(fig3, use_container_width=True)
        else:
            st.info("GFX info not available for this package.")
    
    # Download
    if st.session_state.log_path and os.path.exists(st.session_state.log_path):
        with open(st.session_state.log_path, "rb") as f:
            st.download_button("⬇ Download CSV log", f,
                               file_name=os.path.basename(st.session_state.log_path),
                               mime="text/csv")
