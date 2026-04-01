# app.py — Unified Android Game Profiler
# Real-time live monitor + 60s session recorder + MCP AI Chat in one Streamlit app
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import subprocess, time, re, os, json
from datetime import datetime


# ─── ADB helpers ──────────────────────────────────────────────
def adb(cmd):
    try:
        result = subprocess.run(
            f"adb shell {cmd}", shell=True,
            capture_output=True, text=True, timeout=5
        )
        return result.stdout
    except Exception:
        return ""


def get_battery():
    raw = adb("dumpsys battery")
    level = re.search(r"level: (\d+)", raw)
    temp = re.search(r"temperature: (\d+)", raw)
    volt = re.search(r"voltage: (\d+)", raw)
    return {
        "battery_pct": int(level.group(1)) if level else None,
        "battery_temp": int(temp.group(1)) / 10 if temp else None,
        "battery_volt": int(volt.group(1)) / 1000 if volt else None,
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
    layer_name = None
    try:
        layers = adb("dumpsys SurfaceFlinger --list").splitlines()
        for line in layers:
            if pkg in line and "SurfaceView" in line:
                layer_name = line.strip()
                break
        if not layer_name:
            for line in layers:
                if pkg in line:
                    layer_name = line.strip()
                    break
    except:
        pass

    fps_est = 0.0
    janky = 0
    total = 0

    if layer_name:
        adb(f'dumpsys SurfaceFlinger --latency-clear "{layer_name}"')
        time.sleep(0.5)
        raw = adb(f'dumpsys SurfaceFlinger --latency "{layer_name}"')
        lines = raw.strip().splitlines()
        valid = []
        if len(lines) > 1:
            try:
                refresh_period = int(lines[0])
            except:
                refresh_period = 16666666
            janky_threshold = int(refresh_period * 1.5)
            for i in range(1, len(lines)):
                vals = lines[i].split()
                if len(vals) >= 3:
                    try:
                        actual = int(vals[1])
                        if 0 < actual < 9223372036854775807:
                            valid.append(actual)
                    except:
                        pass

        if len(valid) > 1:
            diff_ns = valid[-1] - valid[0]
            if diff_ns > 0:
                fps_est = float(f"{(len(valid) - 1) * 1e9 / diff_ns:.1f}")
            total = len(valid)
            for i in range(1, len(valid)):
                if valid[i] - valid[i - 1] > janky_threshold:
                    janky += 1

    if total == 0:
        adb(f"dumpsys gfxinfo {pkg} reset")
        raw_flip1 = adb("service call SurfaceFlinger 1013")
        flip1 = None
        match1 = re.search(r"Result: Parcel\([\w]+\s+([\w]+)", raw_flip1)
        if match1:
            try:
                flip1 = int(match1.group(1), 16)
            except:
                pass

        time.sleep(0.5)

        raw_flip2 = adb("service call SurfaceFlinger 1013")
        flip2 = None
        match2 = re.search(r"Result: Parcel\([\w]+\s+([\w]+)", raw_flip2)
        if match2:
            try:
                flip2 = int(match2.group(1), 16)
            except:
                pass

        raw = adb(f"dumpsys gfxinfo {pkg} framestats")
        j_match = re.search(r"Janky frames: (\d+)", raw)
        t_match = re.search(r"Total frames rendered: (\d+)", raw)
        lines = [l for l in raw.splitlines() if l.count(',') >= 13]

        janky = int(j_match.group(1)) if j_match else 0
        total = int(t_match.group(1)) if t_match else 0

        if lines:
            try:
                intervals = []
                prev_vsync = -1
                for line in lines:
                    vals = line.strip().split(',')
                    if len(vals) >= 14 and vals[1].isdigit():
                        vsync = int(vals[1])
                        if prev_vsync != -1 and vsync > prev_vsync:
                            diff = vsync - prev_vsync
                            if 0 < diff < 1000000000:
                                intervals.append(int(diff))
                        prev_vsync = vsync
                if intervals:
                    avg_ns = sum(intervals) / len(intervals)
                    if avg_ns > 0:
                        fps_est = float(f"{1e9 / avg_ns:.1f}")
            except:
                pass

        if fps_est == 0.0 and flip1 is not None and flip2 is not None:
            if flip2 >= flip1:
                fps_est = float((flip2 - flip1) * 2)
                if total == 0:
                    total = (flip2 - flip1)

    return {
        "janky_frames": janky,
        "total_frames": total,
        "fps_est": fps_est,
    }


def get_thermals():
    raw = adb('"cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"')
    temps = [int(t) / 1000 for t in raw.split() if t.isdigit() and int(t) < 200000]
    return {"max_thermal_c": max(temps) if temps else None}


def get_foreground_pkg():
    raw = adb('"dumpsys activity | grep mResumedActivity"')
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


def collect_quick_snapshot(pkg):
    """Lighter snapshot for real-time view."""
    row = {}
    row.update(get_battery())
    row.update(get_memory(pkg))
    row.update(get_cpu(pkg))
    row.update(get_fps(pkg))
    row.update(get_thermals())
    return row


# ─── MCP-style AI Tool Router ────────────────────────────────
# This simulates what an MCP client does: maps natural language
# queries to the correct tool, runs it, and returns a smart response.

def get_device_health():
    """MCP Tool: Returns device thermal, cpu, and screen status."""
    temp_raw = adb('"dumpsys battery | grep temperature"')
    cpu_raw = adb('"top -n 1 -m 1 | grep %"')
    screen_raw = adb('"dumpsys display | grep mScreenState"')
    temp_val = temp_raw.split()[-1] if temp_raw.strip() else "0"
    return {
        "temp": f"{int(temp_val) / 10}°C" if temp_val.isdigit() else "N/A",
        "cpu": cpu_raw.strip()[:80] if cpu_raw.strip() else "Idle",
        "status": "Active" if "ON" in screen_raw.upper() else "Frozen/Off"
    }


def get_device_fps_mcp(package_name):
    """MCP Tool: Returns janky and total frames for a package."""
    package_name = package_name.strip()
    raw = adb(f"dumpsys gfxinfo {package_name}")
    janky = re.search(r"Janky frames: (\d+)", raw)
    total = re.search(r"Total frames rendered: (\d+)", raw)
    return {
        "package": package_name,
        "janky_frames": int(janky.group(1)) if janky else 0,
        "total_frames": int(total.group(1)) if total else 0,
    }


AVAILABLE_TOOLS = {
    "get_device_health": {
        "function": get_device_health,
        "description": "Returns device thermal temp, CPU load, and screen status.",
        "keywords": ["health", "temperature", "temp", "thermal", "screen", "status",
                     "cpu", "device", "hot", "heating", "alive", "on", "off", "frozen"],
        "needs_pkg": False,
    },
    "get_device_fps": {
        "function": get_device_fps_mcp,
        "description": "Returns janky and total frames rendered for a specific game package.",
        "keywords": ["fps", "frames", "janky", "stutter", "lag", "performance",
                     "smooth", "frame", "render", "jank", "drop"],
        "needs_pkg": True,
    },
}


def route_query(query, pkg):
    """Simulates MCP routing: figures out which tool to call based on the query."""
    query_lower = query.lower()

    # Check if user wants both/all
    wants_all = any(w in query_lower for w in ["everything", "all", "full", "complete", "summary", "overview"])

    matched_tools = []
    for tool_name, tool_info in AVAILABLE_TOOLS.items():
        if any(kw in query_lower for kw in tool_info["keywords"]):
            matched_tools.append(tool_name)

    if wants_all or len(matched_tools) == 0:
        matched_tools = list(AVAILABLE_TOOLS.keys())

    results = {}
    tools_called = []
    for tool_name in matched_tools:
        tool = AVAILABLE_TOOLS[tool_name]
        tools_called.append(tool_name)
        if tool["needs_pkg"]:
            results[tool_name] = tool["function"](pkg)
        else:
            results[tool_name] = tool["function"]()

    return results, tools_called


def format_ai_response(results, tools_called):
    """Formats tool results into a friendly AI-style response."""
    parts = []
    parts.append("🤖 **AI Device Monitor Response**\n")
    parts.append(f"*Tools executed:* `{'`, `'.join(tools_called)}`\n")

    if "get_device_health" in results:
        h = results["get_device_health"]
        parts.append("---")
        parts.append("#### 📱 Device Health")
        parts.append(f"- **Temperature:** {h['temp']}")
        parts.append(f"- **Screen:** {h['status']}")
        parts.append(f"- **CPU:** {h['cpu']}")

        if "Frozen" in h["status"] or "Off" in h["status"]:
            parts.append("\n⚠️ *Warning: The device screen appears to be off or frozen!*")

    if "get_device_fps" in results:
        f = results["get_device_fps"]
        parts.append("---")
        parts.append("#### 🎞️ Frame Performance")
        parts.append(f"- **Package:** `{f['package']}`")
        parts.append(f"- **Total Frames Rendered:** {f['total_frames']}")
        parts.append(f"- **Janky Frames:** {f['janky_frames']}")

        if f["total_frames"] > 0 and f["janky_frames"] > 0:
            jank_pct = (f["janky_frames"] / f["total_frames"]) * 100
            parts.append(f"- **Jank Rate:** {jank_pct:.1f}%")
            if jank_pct > 10:
                parts.append(f"\n⚠️ *High jank rate ({jank_pct:.1f}%)! The game may feel stuttery.*")
            else:
                parts.append(f"\n✅ *Jank rate is acceptable ({jank_pct:.1f}%). Game should feel smooth.*")
        elif f["total_frames"] == 0:
            parts.append("\n⚠️ *No frames detected. Is the game actively running on the device screen?*")

    parts.append("---")
    parts.append("*Raw JSON output:*")
    parts.append(f"```json\n{json.dumps(results, indent=2)}\n```")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═══════════════════════════════════════════════════════════════
st.set_page_config(page_title="Android Game Profiler", layout="wide")
st.title("🎮 Android Game Profiler")
st.caption("LnT × Meta Internship Tool | Real-time Monitor + Session Recorder + AI Chat via ADB & MCP")

# ─── Session state init ──────────────────────────────────────
defaults = {
    "running": False, "data": [], "pkg": "jp.konami.pesam",
    "log_path": None, "live_history": [], "chat_messages": [],
}
for key, default in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ─── Sidebar controls ────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuration")

    # Mode selector (replaces tabs to avoid rerun conflicts)
    mode = st.radio("📌 Select Mode",
                    ["📡 Real-Time Monitor", "🎬 Session Recorder", "🤖 AI Chat (MCP)"],
                    index=0)

    st.divider()

    auto_detect = st.toggle("Auto-detect game", value=False)

    if auto_detect:
        if st.button("🔍 Detect foreground app"):
            detected = get_foreground_pkg()
            st.session_state.pkg = detected or ""
            st.success(f"Detected: {detected}" if detected else "Nothing detected")
        pkg = st.session_state.pkg or ""
    else:
        pkg = st.text_input("Package name", value=st.session_state.pkg,
                            placeholder="jp.konami.pesam")
        st.session_state.pkg = pkg

    duration = st.slider("Recording duration (sec)", 10, 120, 60)
    poll_interval = st.selectbox("Poll interval (sec)", [0.5, 1, 2], index=1)

    st.divider()
    st.markdown("**📡 ADB Status**")
    if st.button("Check ADB connection"):
        out = subprocess.run("adb devices", shell=True, capture_output=True, text=True)
        st.code(out.stdout)

# ══════════════════════════════════════════════════════════════
# MODE 1: REAL-TIME LIVE MONITOR
# ══════════════════════════════════════════════════════════════
if mode == "📡 Real-Time Monitor":
    st.subheader("📡 Live Device Monitor")
    if not pkg:
        st.warning("⚠️ Enter a package name in the sidebar first!")
    else:
        st.info(f"Monitoring **{pkg}** — auto-refreshing every **{poll_interval}s**")

        with st.spinner("Polling device..."):
            live = collect_quick_snapshot(pkg)

        # Live KPI Cards
        c1, c2, c3, c4, c5, c6 = st.columns(6)

        batt_pct = live.get("battery_pct")
        batt_temp = live.get("battery_temp")
        cpu_pct = live.get("cpu_pct", 0.0)
        mem_pss = live.get("mem_pss_mb")
        fps = live.get("fps_est", 0.0)
        thermal = live.get("max_thermal_c")

        c1.metric("🔋 Battery", f"{batt_pct}%" if batt_pct is not None else "N/A")
        c2.metric("🌡️ Batt Temp", f"{batt_temp:.1f}°C" if batt_temp is not None else "N/A")
        c3.metric("⚡ CPU", f"{cpu_pct:.1f}%")
        c4.metric("💾 RAM (PSS)", f"{mem_pss:.0f} MB" if mem_pss is not None else "N/A")
        c5.metric("🎞️ FPS", f"{fps:.1f}")
        c6.metric("🔥 Max Thermal", f"{thermal:.1f}°C" if thermal is not None else "N/A")

        janky = live.get("janky_frames", 0)
        total_frames = live.get("total_frames", 0)
        if janky > 0:
            st.warning(f"⚠️ Janky frames: **{janky}** / **{total_frames}** total")

        screen_raw = adb('"dumpsys display | grep mScreenState"')
        if "OFF" in screen_raw.upper():
            st.error("⚠️ ALERT: Device Screen is Inactive!")
        else:
            st.success("✅ Device Screen is ON")

        # Append to rolling history
        live["time"] = datetime.now().strftime("%H:%M:%S")
        st.session_state.live_history.append(live)
        if len(st.session_state.live_history) > 60:
            st.session_state.live_history = st.session_state.live_history[-60:]

        # Rolling Charts
        if len(st.session_state.live_history) > 1:
            hist_df = pd.DataFrame(st.session_state.live_history)

            st.divider()
            st.markdown("##### 📈 Rolling History (last 60 polls)")

            lt1, lt2 = st.columns(2)

            with lt1:
                fig_live1 = go.Figure()
                if "fps_est" in hist_df.columns:
                    fig_live1.add_trace(go.Scatter(
                        x=hist_df["time"], y=hist_df["fps_est"],
                        name="FPS", line={"color": "#f1c40f", "width": 2},
                        fill="tozeroy"))
                if "cpu_pct" in hist_df.columns:
                    fig_live1.add_trace(go.Scatter(
                        x=hist_df["time"], y=hist_df["cpu_pct"],
                        name="CPU %", line={"color": "#3498db", "width": 2}))
                fig_live1.update_layout(
                    title="FPS & CPU", height=300,
                    xaxis_title="Time", yaxis_title="Value",
                    legend=dict(x=0, y=1))
                st.plotly_chart(fig_live1, use_container_width=True)

            with lt2:
                fig_live2 = go.Figure()
                if "battery_temp" in hist_df.columns:
                    fig_live2.add_trace(go.Scatter(
                        x=hist_df["time"], y=hist_df["battery_temp"],
                        name="Batt Temp °C", line={"color": "#e74c3c", "width": 2}))
                if "max_thermal_c" in hist_df.columns:
                    fig_live2.add_trace(go.Scatter(
                        x=hist_df["time"], y=hist_df["max_thermal_c"],
                        name="Max Thermal °C", line={"color": "#e67e22", "width": 2}))
                if "mem_pss_mb" in hist_df.columns:
                    fig_live2.add_trace(go.Scatter(
                        x=hist_df["time"], y=hist_df["mem_pss_mb"],
                        name="RAM PSS (MB)", line={"color": "#9b59b6", "width": 2},
                        yaxis="y2"))
                fig_live2.update_layout(
                    title="Thermals & RAM", height=300,
                    xaxis_title="Time", yaxis_title="Temp °C",
                    yaxis2=dict(overlaying="y", side="right", title="MB"),
                    legend=dict(x=0, y=1))
                st.plotly_chart(fig_live2, use_container_width=True)

        # Auto-refresh (only in live mode)
        time.sleep(poll_interval)
        st.rerun()


# ══════════════════════════════════════════════════════════════
# MODE 2: 60-SECOND SESSION RECORDER
# ══════════════════════════════════════════════════════════════
elif mode == "🎬 Session Recorder":
    st.subheader("🎬 Session Recorder")
    if not pkg:
        st.warning("⚠️ Enter a package name in the sidebar first!")
    else:
        st.info(f"Record **{int(duration)}s** of performance data for **{pkg}** at **{poll_interval}s** intervals.")

        # Controls
        rc1, rc2, _ = st.columns([2, 2, 4])
        with rc1:
            start_btn = st.button("▶ Start Recording", type="primary",
                                  disabled=not pkg or st.session_state.running)
        with rc2:
            stop_btn = st.button("⏹ Stop", disabled=not st.session_state.running)

        if start_btn and pkg:
            st.session_state.running = True
            st.session_state.data = []
            progress = st.progress(0, text="Starting capture...")
            status = st.empty()

            num_samples = int(duration / poll_interval)
            for i in range(num_samples):
                snap = collect_snapshot(pkg, datetime.now().isoformat())
                st.session_state.data.append(snap)
                pct = (i + 1) / num_samples
                progress.progress(pct, text=f"Capturing... {i + 1}/{num_samples} samples")
                time.sleep(poll_interval)

            st.session_state.running = False
            df = pd.DataFrame(st.session_state.data)
            fname = f"game_profile_{pkg}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            df.to_csv(fname, index=False)
            st.session_state.log_path = fname
            status.success(f"✅ Capture complete! Saved to `{fname}`")

        # Results dashboard
        if st.session_state.data:
            df = pd.DataFrame(st.session_state.data)
            df["time_s"] = range(len(df))

            st.divider()
            st.subheader("📊 Session Summary")

            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Avg Battery %", f"{df['battery_pct'].mean():.1f}%",
                      f"{df['battery_pct'].iloc[-1] - df['battery_pct'].iloc[0]:+.0f}% drain")
            m2.metric("Max Temp (°C)", f"{df['battery_temp'].max():.1f}°",
                      f"avg {df['battery_temp'].mean():.1f}°")
            m3.metric("Peak CPU %", f"{df['cpu_pct'].max():.1f}%",
                      f"avg {df['cpu_pct'].mean():.1f}%")
            m4.metric("Peak RAM (MB)", f"{df['mem_pss_mb'].max():.0f} MB",
                      f"avg {df['mem_pss_mb'].mean():.0f} MB")
            m5.metric("Max Thermal (°C)", f"{df['max_thermal_c'].max():.1f}°",
                      f"avg {df['max_thermal_c'].mean():.1f}°")
            m6.metric("Avg FPS", f"{df['fps_est'].mean():.1f}",
                      f"peak {df['fps_est'].max():.1f}")

            tab1, tab2, tab3 = st.tabs(["🔋 Battery & Temp", "⚡ CPU & RAM", "🎞 FPS & Frames"])

            with tab1:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df["time_s"], y=df["battery_pct"],
                                         name="Battery %", line={"color": "#2ecc71", "width": 2}))
                fig.add_trace(go.Scatter(x=df["time_s"], y=df["battery_temp"],
                                         name="Temp (°C)", line={"color": "#e74c3c", "width": 2},
                                         yaxis="y2"))
                fig.update_layout(yaxis2=dict(overlaying="y", side="right", title="Temp °C"),
                                  xaxis_title="Time (s)", yaxis_title="Battery %",
                                  legend=dict(x=0, y=1), height=360)
                st.plotly_chart(fig, use_container_width=True)

            with tab2:
                fig2 = go.Figure()
                fig2.add_trace(go.Scatter(x=df["time_s"], y=df["cpu_pct"],
                                          name="CPU %", fill="tozeroy", line={"color": "#3498db"}))
                fig2.add_trace(go.Scatter(x=df["time_s"], y=df["mem_pss_mb"],
                                          name="RAM PSS (MB)", line={"color": "#9b59b6"},
                                          yaxis="y2"))
                fig2.update_layout(yaxis2=dict(overlaying="y", side="right", title="MB"),
                                   xaxis_title="Time (s)", yaxis_title="CPU %", height=360)
                st.plotly_chart(fig2, use_container_width=True)

            with tab3:
                if "janky_frames" in df.columns and "fps_est" in df.columns:
                    fig3 = go.Figure()
                    fig3.add_trace(go.Scatter(x=df["time_s"], y=df["fps_est"],
                                              name="FPS", line={"color": "#f1c40f", "width": 2}))
                    fig3.add_trace(go.Bar(x=df["time_s"], y=df["janky_frames"],
                                          name="Janky Frames", marker_color="#e74c3c", opacity=0.6,
                                          yaxis="y2"))
                    fig3.update_layout(yaxis2=dict(overlaying="y", side="right", title="Janky Frames"),
                                       xaxis_title="Time (s)", yaxis_title="FPS",
                                       legend=dict(x=0, y=1), height=360)
                    st.plotly_chart(fig3, use_container_width=True)
                elif "janky_frames" in df.columns:
                    fig3 = px.bar(df, x="time_s", y="janky_frames",
                                  color="janky_frames", color_continuous_scale="Reds",
                                  labels={"time_s": "Time (s)", "janky_frames": "Janky Frames"})
                    st.plotly_chart(fig3, use_container_width=True)
                else:
                    st.info("GFX info not available for this package.")

            if st.session_state.log_path and os.path.exists(st.session_state.log_path):
                with open(st.session_state.log_path, "rb") as f:
                    st.download_button("⬇ Download CSV log", f,
                                       file_name=os.path.basename(st.session_state.log_path),
                                       mime="text/csv")


# ══════════════════════════════════════════════════════════════
# MODE 3: AI CHAT (MCP TOOLS)
# ══════════════════════════════════════════════════════════════
elif mode == "🤖 AI Chat (MCP)":
    st.subheader("🤖 AI Device Monitor Chat")
    st.markdown("""
    Ask me anything about your connected Android device in **plain English**!  
    I will automatically route your question to the correct MCP tool, execute it via ADB, 
    and return a formatted response — just like Claude Desktop would.
    
    **Example queries:**
    - *"Check the device health"*
    - *"How many FPS is my game running at?"*
    - *"Is the screen on?"*
    - *"Give me everything"*
    """)

    st.divider()

    # Show available tools
    with st.expander("🔧 Available MCP Tools"):
        for name, info in AVAILABLE_TOOLS.items():
            st.markdown(f"**`{name}`** — {info['description']}")
            st.caption(f"Trigger keywords: {', '.join(info['keywords'][:6])}...")

    # Chat history display
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    if prompt := st.chat_input("Ask about your device... (e.g. 'check fps', 'device health', 'give me everything')"):
        # Show user message
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Process query through MCP tool router
        with st.chat_message("assistant"):
            with st.spinner("🔍 Routing query to MCP tools & polling device via ADB..."):
                results, tools_called = route_query(prompt, pkg)
                response = format_ai_response(results, tools_called)
            st.markdown(response)

        st.session_state.chat_messages.append({"role": "assistant", "content": response})

    # Clear chat button
    if st.session_state.chat_messages:
        if st.button("🗑️ Clear Chat"):
            st.session_state.chat_messages = []
            st.rerun()
