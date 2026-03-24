# app.py — Unified Android Game Profiler
# Real-time live monitor + 60s Session Recorder (Perfetto) + MCP AI Chat
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import subprocess, time, re, os, json, threading
from datetime import datetime
from dotenv import load_dotenv
import openai
from perfetto.trace_processor import TraceProcessor

# Load environment variables
load_dotenv()

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
    temp  = re.search(r"temperature: (\d+)", raw)
    volt  = re.search(r"voltage: (\d+)", raw)
    return {
        "battery_pct":  int(level.group(1)) if level else None,
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

def get_thermals():
    raw = adb('"cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"')
    temps = [int(t)/1000 for t in raw.split() if t.isdigit() and int(t) < 200000]
    return {"max_thermal_c": max(temps) if temps else None}

def get_foreground_pkg():
    raw = adb('"dumpsys activity | grep mResumedActivity"')
    match = re.search(r"u0 ([\w.]+)/", raw)
    return match.group(1) if match else None

def collect_snapshot(pkg, ts):
    # Standard lightweight ADB polling (CPU/RAM/Temp)
    row = {"timestamp": ts}
    row.update(get_battery())
    row.update(get_memory(pkg))
    row.update(get_cpu(pkg))
    row.update(get_thermals())
    return row

# ─── Perfetto Tracing Helpers ─────────────────────────────────
def run_perfetto_trace(pkg, duration_sec):
    config = f"""
buffers {{ size_kb: 126976 fill_policy: RING_BUFFER }}
data_sources {{ config {{ name: "android.surfaceflinger.frametimeline" }} }}
data_sources {{ config {{ name: "linux.ftrace"
        ftrace_config {{
            ftrace_events: "ftrace/print"
            ftrace_events: "power/cpu_frequency"
            ftrace_events: "power/cpu_idle"
            atrace_categories: "gfx"
            atrace_categories: "view"
            atrace_categories: "wm"
            atrace_categories: "sched"
            atrace_categories: "freq"
            atrace_categories: "rs"
            atrace_categories: "am"
            atrace_apps: "{pkg}"
            atrace_apps: "*"
        }}
    }} }}
data_sources {{ config {{ name: "linux.process_stats"
        process_stats_config {{ scan_all_processes_on_start: true proc_stats_poll_ms: 1000 }}
    }} }}
duration_ms: {duration_sec * 1000}
"""
    with open("perfetto_config.pbtx", "w") as f:
        f.write(config)
    
    subprocess.run("adb push perfetto_config.pbtx /data/local/tmp/perfetto_config.pbtx", shell=True)
    subprocess.run("adb shell \"cat /data/local/tmp/perfetto_config.pbtx | perfetto --txt -c - -o /data/misc/perfetto-traces/trace.perfetto-trace\"", shell=True)
    
    trace_path = f"trace_{pkg}_{int(time.time())}.perfetto-trace"
    subprocess.run(f"adb pull /data/misc/perfetto-traces/trace.perfetto-trace {trace_path}", shell=True)
    return trace_path

def parse_perfetto_trace(trace_path, pkg):
    try:
        tp = TraceProcessor(trace=trace_path)
    except Exception as e:
        st.error(f"Failed to initialize trace processor: {e}")
        return pd.DataFrame(), "init_error"

    # ── Step 1: Check if frametimeline data exists at all ──
    try:
        count_row = tp.query(
            "SELECT COUNT(*) as c FROM actual_frame_timeline_slice"
        ).as_pandas_dataframe()
        has_frametimeline = count_row.iloc[0]['c'] > 0
    except Exception:
        has_frametimeline = False

    df_results = pd.DataFrame()
    method_used = "none"

    # ── Step 2A: Preferred — SurfaceFlinger FrameTimeline ──
    if has_frametimeline:
        try:
            # Find dominant game layer automatically
            # Handle Unity 'None' layers (NULL in SQL)
            layer_df = tp.query("""
                SELECT 
                  COALESCE(layer_name, 'None') as layer_name,
                  COUNT(*) as c
                FROM actual_frame_timeline_slice
                WHERE present_type = 'PRESENTED'
                  AND (
                    layer_name IS NULL
                    OR (
                      layer_name NOT LIKE '%StatusBar%'
                      AND layer_name NOT LIKE '%NavigationBar%'
                      AND layer_name NOT LIKE '%TaskBar%'
                      AND layer_name NOT LIKE '%InputMethod%'
                      AND layer_name NOT LIKE '%Snapshot%'
                      AND layer_name NOT LIKE '%Wallpaper%'
                    )
                  )
                GROUP BY layer_name
                ORDER BY c DESC
                LIMIT 1
            """).as_pandas_dataframe()

            if not layer_df.empty:
                dominant = layer_df.iloc[0]['layer_name']
                if dominant == "None":
                    layer_filter = "layer_name IS NULL"
                else:
                    escaped_dominant = dominant.replace("'", "''")
                    layer_filter = f"layer_name = '{escaped_dominant}'"
                
                query = f"""
                WITH app_frames AS (
                  SELECT ts, dur
                  FROM actual_frame_timeline_slice
                  WHERE present_type = 'PRESENTED'
                    AND {layer_filter}
                ),
                min_ts AS (SELECT MIN(ts) as start_ts FROM app_frames),
                bucketed AS (
                  SELECT
                    CAST((ts-(SELECT start_ts FROM min_ts))/1e9 AS INT) AS time_s,
                    ts/1e6  AS ts_ms,
                    dur/1e6 AS dur_ms,
                    CASE WHEN dur > 16666666 THEN 1 ELSE 0 END AS is_jank
                  FROM app_frames
                )
                SELECT
                  time_s, ts_ms, dur_ms, is_jank,
                  COUNT(*) OVER (PARTITION BY time_s) AS fps_at_sec
                FROM bucketed ORDER BY ts_ms
                """
                df_results = tp.query(query).as_pandas_dataframe()
                method_used = f"frametimeline:{dominant[:40]}"
        except Exception as e:
            print(f"FrameTimeline query failed: {e}")

    # ── Step 2B: Fallback — atrace, ONE slice type only (queueBuffer = 1 per frame) ──
    if df_results.empty:
        try:
            short_pkg = pkg.split('.')[-1]
            # CRITICAL: use ONLY queueBuffer — it fires exactly once per submitted frame.
            # Never mix multiple slice names; each adds its own count, inflating FPS.
            query = f"""
            WITH app_slices AS (
              SELECT s.ts, s.dur
              FROM slice s
              JOIN thread_track tt ON s.track_id = tt.id
              JOIN thread t        ON tt.utid = t.utid
              JOIN process p       ON t.upid = p.upid
              WHERE p.name LIKE '%{short_pkg}%'
                AND s.name = 'queueBuffer'
                AND s.dur > 0
            ),
            min_ts AS (SELECT MIN(ts) as start_ts FROM app_slices),
            bucketed AS (
              SELECT
                CAST((ts-(SELECT start_ts FROM min_ts))/1e9 AS INT) AS time_s,
                ts/1e6  AS ts_ms,
                dur/1e6 AS dur_ms,
                CASE WHEN dur > 16666666 THEN 1 ELSE 0 END AS is_jank
              FROM app_slices
            )
            SELECT time_s, ts_ms, dur_ms, is_jank,
              COUNT(*) OVER (PARTITION BY time_s) AS fps_at_sec
            FROM bucketed ORDER BY ts_ms
            """
            df_results = tp.query(query).as_pandas_dataframe()
            # If queueBuffer gave nothing, try Choreographer#doFrame (exactly 1 per VSYNC)
            if df_results.empty:
                query2 = f"""
                WITH app_slices AS (
                  SELECT s.ts, s.dur
                  FROM slice s
                  JOIN thread_track tt ON s.track_id = tt.id
                  JOIN thread t        ON tt.utid = t.utid
                  JOIN process p       ON t.upid = p.upid
                  WHERE p.name LIKE '%{short_pkg}%'
                    AND s.name = 'Choreographer#doFrame'
                    AND s.dur > 0
                ),
                min_ts AS (SELECT MIN(ts) as start_ts FROM app_slices),
                bucketed AS (
                  SELECT
                    CAST((ts-(SELECT start_ts FROM min_ts))/1e9 AS INT) AS time_s,
                    ts/1e6  AS ts_ms,
                    dur/1e6 AS dur_ms,
                    CASE WHEN dur > 16666666 THEN 1 ELSE 0 END AS is_jank
                  FROM app_slices
                )
                SELECT time_s, ts_ms, dur_ms, is_jank,
                  COUNT(*) OVER (PARTITION BY time_s) AS fps_at_sec
                FROM bucketed ORDER BY ts_ms
                """
                df_results = tp.query(query2).as_pandas_dataframe()
                method_used = "atrace:Choreographer"
            else:
                method_used = "atrace:queueBuffer"
        except Exception as e:
            print(f"atrace fallback failed: {e}")

    # ── Step 2C: Last resort — count any GPU work slices ──
    if df_results.empty:
        try:
            short_pkg = pkg.split('.')[-1]
            query = f"""
            WITH gpu_slices AS (
              SELECT ts, dur
              FROM slice
              JOIN thread_track ON slice.track_id = thread_track.id
              JOIN thread USING(utid)
              JOIN process USING(upid)
              WHERE process.name LIKE '%{short_pkg}%'
                AND dur > 1000000
            ),
            min_ts AS (SELECT MIN(ts) as start_ts FROM gpu_slices),
            bucketed AS (
              SELECT
                CAST((ts-(SELECT start_ts FROM min_ts))/1e9 AS INT) AS time_s,
                ts/1e6  AS ts_ms,
                dur/1e6 AS dur_ms,
                0 AS is_jank
              FROM gpu_slices
            )
            SELECT time_s, ts_ms, dur_ms, is_jank,
              COUNT(*) OVER (PARTITION BY time_s) AS fps_at_sec
            FROM bucketed ORDER BY ts_ms
            """
            df_results = tp.query(query).as_pandas_dataframe()
            method_used = "gpu_slices_fallback"
        except Exception as e:
            print(f"GPU slice fallback failed: {e}")

    tp.close()
    return df_results, method_used

# ─── MCP-style AI Tool Router ────────────────────────────────
def get_device_health():
    temp_raw = adb('"dumpsys battery | grep temperature"')
    cpu_raw = adb('"top -n 1 -m 1 | grep %"')
    screen_raw = adb('"dumpsys display | grep mScreenState"')
    temp_val = temp_raw.split()[-1] if temp_raw.strip() else "0"
    return {
        "temp": f"{int(temp_val)/10}°C" if temp_val.isdigit() else "N/A",
        "cpu": cpu_raw.strip()[:80] if cpu_raw.strip() else "Idle",
        "status": "Active" if "ON" in screen_raw.upper() else "Frozen/Off"
    }

def get_cpu_mcp(package_name):
    return get_cpu(package_name)

def get_memory_mcp(package_name):
    return get_memory(package_name)

def get_battery_mcp():
    return get_battery()

def get_thermal_mcp():
    return get_thermals()

def get_fps_mcp(package_name):
    package_name = package_name.strip()
    # Resetting gfxinfo and waiting for a fresh 1.5s window ensures the AI
    # sees the actual current performance rather than accumulated stale counters.
    adb(f"dumpsys gfxinfo {package_name} reset")
    time.sleep(1.5)
    raw = adb(f"dumpsys gfxinfo {package_name}")
    janky = re.search(r"Janky frames: (\d+)", raw)
    total = re.search(r"Total frames rendered: (\d+)", raw)
    return {
        "janky_frames": int(janky.group(1)) if janky else 0,
        "total_frames": int(total.group(1)) if total else 0,
    }

def analyze_performance_engine(package_name):
    """AI Diagnosis Engine: Run full diagnostic and detect bottlenecks natively"""
    data = collect_snapshot(package_name, datetime.now().isoformat())
    current_pkg = get_foreground_pkg()
    issues = []
    status = "running"
    
    if current_pkg != package_name:
        status = "not_running_or_background"
        issues.append(f"Game is not in foreground (Current: {current_pkg})")
    
    if data.get("cpu_pct", 0) > 80: issues.append("High CPU usage")
    if data.get("battery_temp") and data["battery_temp"] > 40: issues.append("Thermal throttling risk")
    if data.get("mem_pss_mb", 0) > 3000: issues.append("High Memory usage (RAM bottleneck)")
    
    fps = get_fps_mcp(package_name)
    if status == "running" and fps.get("janky_frames", 0) > 5:
        issues.append("High frame drops (jank detected)")
    
    return {
        "status": status,
        "foreground_app": current_pkg,
        "raw_snapshot": data,
        "diagnosed_issues": issues if issues else ["No major bottlenecks detected. Device is running smoothly."]
    }

SYSTEM_PROMPT = """
You are an Android Game Performance Expert.

You will be given real-time device metrics (CPU, RAM, FPS, Thermal) and diagnostic issues.
Analyze the data provided and:
1. Identify specific bottlenecks (e.g., thermal throttling, high memory swap, CPU spikes).
2. Explain the impact on the user's experience (e.g., "The lag is caused by thermal throttling as the battery temp is over 42°C").
3. Suggest concrete fixes (e.g., lower game graphics settings, close background apps).

BE CONCISE and TECHNICAL.
"""


AVAILABLE_TOOLS = {
    "get_cpu_usage": {"function": lambda p: get_cpu_mcp(p), "description": "Get current CPU % load for the game.", "keywords": ["cpu", "load"]},
    "get_memory_usage": {"function": lambda p: get_memory_mcp(p), "description": "Get RAM/Memory PSS usage.", "keywords": ["ram", "memory", "leak"]},
    "get_fps": {"function": lambda p: get_fps_mcp(p), "description": "Get total frames and janky stutters.", "keywords": ["fps", "jank", "stutter", "lag", "frames"]},
    "get_thermal": {"function": lambda _: get_thermal_mcp(), "description": "Get max thermal temperature.", "keywords": ["temp", "thermal", "overheating", "hot"]},
    "get_battery": {"function": lambda _: get_battery_mcp(), "description": "Get battery % and temperature.", "keywords": ["battery", "power"]},
    "analyze_performance": {"function": lambda p: analyze_performance_engine(p), "description": "Run the AI Lag Diagnosis Engine to detect bottlenecks.", "keywords": ["diagnose", "analyze", "why", "lagging", "issue"]}
}

def route_query(query, pkg):
    query_lower = query.lower()
    matched_tools = [name for name, info in AVAILABLE_TOOLS.items() if any(kw in query_lower for kw in info["keywords"])]
    if not matched_tools: matched_tools = ["analyze_performance"]
    
    results = {}
    for tool_name in matched_tools:
        results[tool_name] = AVAILABLE_TOOLS[tool_name]["function"](pkg)
    return results, matched_tools

def format_ai_response(results, tools_called):
    # Extract status for the header
    analysis = results.get("analyze_performance", {})
    status = analysis.get("status", "unknown")
    foreground = analysis.get("foreground_app", "unknown")
    
    parts = [
        "🤖 **Android Performance Analyst**\n",
        f"📱 Foreground App: `{foreground}`",
        f"🎮 Game Status: **{status.upper()}**\n",
        f"*Tools executed:* `{'`, `'.join(tools_called)}`\n",
        "---",
        "*Raw Diagnostic Data:*",
        f"```json\n{json.dumps(results, indent=2)}\n```"
    ]
    return "\n".join(parts)

# ═══════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═══════════════════════════════════════════════════════════════
st.set_page_config(page_title="Android Game Profiler", layout="wide")
st.title("🎮 Android Game Profiler")
st.caption("LnT × Meta Internship Tool | Live Monitor + Perfetto Session Recorder + AI Chat")

defaults = {"running": False, "data": [], "pkg": "jp.konami.pesam", "log_path": None, "live_history": [], "chat_messages": [], "perfetto_data": None}
for key, default in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = default

with st.sidebar:
    st.header("⚙️ Configuration")
    mode = st.radio("📌 Select Mode", ["📡 Real-Time Monitor", "🎬 Perfetto Session Recorder", "🤖 AI Chat (MCP)"], index=0)
    st.divider()
    pkg = st.text_input("Package name", value=st.session_state.pkg)
    st.session_state.pkg = pkg
    duration = st.slider("Recording duration (sec)", 5, 120, 10, help="For Perfetto traces, 10-20s is recommended to avoid gigantic trace files.")
    poll_interval = 1.0
    st.divider()
    st.markdown("**📡 ADB Status**")
    if st.button("Check ADB connection"):
        out = subprocess.run("adb devices", shell=True, capture_output=True, text=True)
        st.code(out.stdout)

# ══════════════════════════════════════════════════════════════
# MODE 1: REAL-TIME LIVE MONITOR (Lightweight)
# ══════════════════════════════════════════════════════════════
if mode == "📡 Real-Time Monitor":
    st.subheader("📡 Live Device Monitor (ADB Polling)")
    if not pkg:
        st.warning("⚠️ Enter a package name first!")
    else:
        with st.spinner("Polling device..."):
            live = collect_snapshot(pkg, datetime.now().isoformat())
            # For live mode, fake FPS to 0 since standard ADB fails for Unity/Native games
            live["fps_est"] = 0 
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🔋 Battery", f"{live.get('battery_pct', 'N/A')}%")
        c2.metric("🌡️ Temp", f"{live.get('battery_temp', 'N/A')}°C")
        c3.metric("⚡ Peak CPU", f"{live.get('cpu_pct', 0.0):.1f}%")
        c4.metric("💾 RAM (PSS)", f"{live.get('mem_pss_mb', 0):.0f} MB" if live.get('mem_pss_mb') else "N/A")
        
        live["time"] = datetime.now().strftime("%H:%M:%S")
        st.session_state.live_history.append(live)
        if len(st.session_state.live_history) > 60:
            st.session_state.live_history = st.session_state.live_history[-60:]
        
        if len(st.session_state.live_history) > 1:
            hist_df = pd.DataFrame(st.session_state.live_history)
            fig_live = go.Figure()
            if "cpu_pct" in hist_df.columns:
                fig_live.add_trace(go.Scatter(x=hist_df["time"], y=hist_df["cpu_pct"], name="CPU %", line={"color": "#3498db", "width": 2}, fill="tozeroy"))
            if "battery_temp" in hist_df.columns:
                fig_live.add_trace(go.Scatter(x=hist_df["time"], y=hist_df["battery_temp"], name="Batt Temp °C", line={"color": "#e74c3c", "width": 2}, yaxis="y2"))
            fig_live.update_layout(title="Hardware Utilization", height=350, yaxis2=dict(overlaying="y", side="right"), legend=dict(x=0, y=1))
            st.plotly_chart(fig_live, use_container_width=True)
        
        time.sleep(poll_interval)
        st.rerun()

# ══════════════════════════════════════════════════════════════
# MODE 2: PERFETTO SESSION RECORDER (Deep Hardware Trace)
# ══════════════════════════════════════════════════════════════
elif mode == "🎬 Perfetto Session Recorder":
    st.subheader("🎬 Deep Session Recorder (Perfetto)")
    st.info("Uses Google's Perfetto trace engine to directly query hardware GPU instructions. This captures perfectly accurate FPS, frametimes, and Janks for Native/Unity/Unreal games.")
    
    col1, col2 = st.columns([2, 8])
    with col1:
        start_btn = st.button("▶ Record Perfetto Trace", type="primary", disabled=not pkg or st.session_state.running)
    
    if start_btn and pkg:
        # Mandatory: Clear ALL stale session state to prevent old trace data display
        st.session_state.running = True
        st.session_state.data = []
        st.session_state.perfetto_data = None
        st.session_state["gfx_summary"] = {}
        st.session_state["session_duration"] = 1
        st.session_state["trace_method"] = ""
        st.session_state["trace_file"] = None
        
        # 1. Start Perfetto as a background process so we don't need Streamlit threads
        with st.spinner(f"🎥 Recording deeply on instrumented native graphics for {duration} seconds..."):
            with open("perfetto_config.pbtx", "w") as f:
                config = f'''buffers {{ size_kb: 126976 fill_policy: RING_BUFFER }} data_sources {{ config {{ name: "android.surfaceflinger.frametimeline" }} }} data_sources {{ config {{ name: "linux.ftrace" ftrace_config {{ ftrace_events: "ftrace/print" ftrace_events: "power/cpu_frequency" ftrace_events: "power/cpu_idle" atrace_categories: "gfx" atrace_categories: "view" atrace_categories: "wm" atrace_categories: "sched" atrace_categories: "freq" atrace_categories: "rs" atrace_categories: "am" atrace_apps: "{pkg}" atrace_apps: "*" }} }} }} data_sources {{ config {{ name: "linux.process_stats" process_stats_config {{ scan_all_processes_on_start: true proc_stats_poll_ms: 1000 }} }} }} duration_ms: {duration * 1000}'''
                f.write(config)
            subprocess.run("adb push perfetto_config.pbtx /data/local/tmp/perfetto_config.pbtx", shell=True)
            
            # Reset gfxinfo before recording
            adb(f"dumpsys gfxinfo {pkg} reset")
            
            start_wall = time.time()
            # Start perfetto asynchronously
            perfetto_proc = subprocess.Popen("adb shell \"cat /data/local/tmp/perfetto_config.pbtx | perfetto --txt -c - -o /data/misc/perfetto-traces/trace.perfetto-trace\"", shell=True)
            
            # 2. Main thread stays alive to poll ADB hardware stats safely!
            adb_results = []
            end_time_limit = time.time() + duration
            while time.time() < end_time_limit:
                snap = collect_snapshot(pkg, datetime.now().isoformat())
                adb_results.append(snap)
                time.sleep(1.0)
            
            # Wait for perfetto to finish its graceful teardown
            perfetto_proc.wait()
            end_wall = time.time()
            st.session_state["session_duration"] = end_wall - start_wall
            
            # Get final gfxinfo stats
            st.session_state["gfx_summary"] = get_fps_mcp(pkg)
            
            # Pull trace
            trace_file = f"trace_{pkg}_{int(time.time())}.perfetto-trace"
            subprocess.run(f"adb pull /data/misc/perfetto-traces/trace.perfetto-trace {trace_file}", shell=True)
            st.session_state["trace_file"] = trace_file

        st.session_state.running = False
        st.session_state.data = adb_results
        
        with st.spinner("🧠 Booting Perfetto SQL Trace Processor & Dissecting Trace..."):
            perfetto_df, method_used = parse_perfetto_trace(trace_file, pkg)
            st.session_state["trace_method"] = method_used
            
            if not perfetto_df.empty:
                st.session_state.perfetto_data = perfetto_df.to_dict('records')
                st.success(f"✅ Perfetto processing complete! Method: `{method_used}`")
            else:
                st.session_state.perfetto_data = "EMPTY"
                st.warning("⚠️ No frame data found. Try: keep the game actively playing during recording.")

    # ─── Dashboard Render ───────────────────
    if st.session_state.data and st.session_state.perfetto_data:
        adb_df = pd.DataFrame(st.session_state.data)
        adb_df["time_s"] = range(len(adb_df))
        
        st.divider()
        st.subheader("📊 Session Summary")
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        
        m1.metric("Batt Drain", f"{adb_df['battery_pct'].iloc[0] - adb_df['battery_pct'].iloc[-1]:.1f}%" if not adb_df['battery_pct'].isnull().all() else "N/A")
        m2.metric("Max Temp", f"{adb_df['battery_temp'].max():.1f}°C" if not adb_df['battery_temp'].isnull().all() else "N/A")
        m3.metric("Peak CPU", f"{adb_df['cpu_pct'].max():.1f}%" if not adb_df['cpu_pct'].isnull().all() else "N/A")
        m4.metric("Avg RAM", f"{adb_df['mem_pss_mb'].mean():.0f}MB" if 'mem_pss_mb' in adb_df.columns and not adb_df['mem_pss_mb'].isnull().all() else "N/A")

        # ── FPS CALCULATION (Perfetto-first, gfxinfo as sanity check) ──
        gfx = st.session_state.get("gfx_summary", {})
        dur = st.session_state.get("session_duration", 1)
        real_fps = 0.0

        # Method 1: Perfetto (most accurate for Unity/native games)
        if st.session_state.perfetto_data != "EMPTY":
            pdf = pd.DataFrame(st.session_state.perfetto_data)
            # Drop first and last second (partial windows skew the average)
            fps_per_sec = pdf.groupby("time_s")["fps_at_sec"].first()
            if len(fps_per_sec) > 2:
                fps_per_sec = fps_per_sec.iloc[1:-1]
            real_fps = round(fps_per_sec.median(), 1) if not fps_per_sec.empty else 0.0

        # Method 2: gfxinfo cross-check (only if Perfetto gave 0)
        # Note: gfxinfo often returns 0 for Unity games — treat as unreliable
        if real_fps == 0.0 and dur > 0 and gfx.get("total_frames", 0) > 5:
            real_fps = round(gfx["total_frames"] / dur, 1)

        # NO hardcoded clamp — respect whatever the game's actual FPS cap is
        # (30, 60, 90, 120 Hz are all valid)

        # Jank: prefer Perfetto data, fall back to gfxinfo
        if st.session_state.perfetto_data != "EMPTY":
            pdf = pd.DataFrame(st.session_state.perfetto_data)
            total_frames_p = len(pdf)
            jank_frames_p  = pdf["is_jank"].sum()
            jank_pct = (jank_frames_p / total_frames_p * 100) if total_frames_p > 0 else 0.0
        elif gfx.get("total_frames"):
            jank_pct = gfx.get("janky_frames", 0) / gfx["total_frames"] * 100
        else:
            jank_pct = 0.0
        
        _method = st.session_state.get("trace_method", "")
        _fps_label = "🎯 Real FPS" if _method.startswith("frametimeline") else "🎯 FPS (atrace)"
        m5.metric(_fps_label, f"{real_fps:.1f}")
        m6.metric("⚠️ Jank %", f"{jank_pct:.1f}%")
        
        with st.expander("🛠️ Advanced Debug: Trace Diagnostics"):
            if st.session_state.perfetto_data != "EMPTY":
                # Per-second FPS table from the already-parsed Perfetto data
                _pdf = pd.DataFrame(st.session_state.perfetto_data)
                if "time_s" in _pdf.columns and "fps_at_sec" in _pdf.columns:
                    fps_table = _pdf.groupby("time_s")["fps_at_sec"].first().reset_index()
                    fps_table.columns = ["time_s", "fps_at_sec"]
                    st.write("**Per-second FPS from trace:**")
                    st.dataframe(fps_table, use_container_width=True)
                
                _tf = st.session_state.get("trace_file", "")
                if not _tf:
                    st.write("No trace file in session yet — re-record to populate trace diagnostics.")
                elif not os.path.exists(_tf):
                    st.write(f"Trace file not found on disk: `{_tf}`")
                else:
                    try:
                        debug_tp = TraceProcessor(trace=_tf)
                        # 1. Check frametimeline count
                        c = debug_tp.query("SELECT COUNT(*) as c FROM actual_frame_timeline_slice").as_pandas_dataframe()
                        st.write(f"**actual_frame_timeline_slice rows:** {c.iloc[0]['c']}")
                        
                        # 2. Show all layers
                        layers = debug_tp.query("""
                            SELECT layer_name, COUNT(*) as frames, present_type
                            FROM actual_frame_timeline_slice
                            GROUP BY layer_name, present_type
                            ORDER BY frames DESC LIMIT 20
                        """).as_pandas_dataframe()
                        st.write("**All FrameTimeline Layers:**", layers)
                        
                        # 3. Show process names with slice counts
                        procs = debug_tp.query("""
                            SELECT process.name as proc_name, COUNT(*) as slice_count
                            FROM process JOIN thread USING(upid)
                            JOIN thread_track ON thread.utid = thread_track.utid
                            JOIN slice ON thread_track.id = slice.track_id
                            GROUP BY proc_name ORDER BY slice_count DESC LIMIT 15
                        """).as_pandas_dataframe()
                        st.write("**Active Processes (with slices):**", procs)
                        debug_tp.close()
                    except Exception as ex:
                        st.error(f"Debug error: {ex}")

        t1, t2, t3 = st.tabs(["🎞 Frame Timeline (Final)", "📉 Latency (ms)", "⚡ Hardware (CPU/RAM)"])
        
        with t1:
            if st.session_state.perfetto_data != "EMPTY":
                pdf = pd.DataFrame(st.session_state.perfetto_data)
                # Apply sanity clamp to individual second points for the graph too
                fps_df = pdf.groupby("time_s")["fps_at_sec"].first().reset_index()
                # No clamp — show real FPS whatever it is (30, 60, 90, 120)
                
                jank_counts = pdf.groupby("time_s")["is_jank"].sum().reset_index()
                
                fig_p = go.Figure()
                fig_p.add_trace(go.Scatter(x=fps_df["time_s"], y=fps_df["fps_at_sec"], name="Display FPS", line={"color": "#2ecc71", "width": 3}, fill="tozeroy"))
                fig_p.add_trace(go.Bar(x=jank_counts["time_s"], y=jank_counts["is_jank"], name="Jank Count", marker_color="#e74c3c", opacity=0.6, yaxis="y2"))
                fig_p.update_layout(title="Presented Frame Timeline (Real-world Display FPS)", yaxis2=dict(overlaying="y", side="right", title="Janks"), height=400)
                st.plotly_chart(fig_p, use_container_width=True)
            else:
                st.error("Trace Processor failed to extract presented frames. Displaying fallback summary only.")

        with t2:
            if st.session_state.perfetto_data != "EMPTY":
                pdf = pd.DataFrame(st.session_state.perfetto_data)
                fig_lat = px.line(pdf, x="ts_ms", y="dur_ms", title="Frame Latency (ms) - Target < 16.6ms for 60FPS", labels={"dur_ms": "Latency (ms)", "ts_ms": "Time (ms)"})
                fig_lat.add_hline(y=16.6, line_dash="dash", line_color="red", annotation_text="60 FPS Limit")
                fig_lat.update_layout(height=400)
                st.plotly_chart(fig_lat, use_container_width=True)
            else:
                st.info("No latency data available for this trace.")
                
        with t3:
            fig2 = go.Figure()
            if "cpu_pct" in adb_df.columns:
                fig2.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["cpu_pct"], name="CPU %", fill="tozeroy"))
            if "mem_pss_mb" in adb_df.columns:
                fig2.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["mem_pss_mb"], name="RAM PSS", yaxis="y2"))
            fig2.update_layout(yaxis2=dict(overlaying="y", side="right"), height=400)
            st.plotly_chart(fig2, use_container_width=True)

# ══════════════════════════════════════════════════════════════
# MODE 3: AI CHAT (MCP)
# ══════════════════════════════════════════════════════════════
elif mode == "🤖 AI Chat (MCP)":
    st.subheader("🤖 AI Device Monitor Chat (Powered by Groq)")
    
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    
    if groq_api_key:
        st.success("✅ Ultra-Fast Groq AI connected.")
    else:
        st.warning("⚠️ No GROQ_API_KEY found. Using fallback analysis.")
    
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]): st.markdown(msg["content"])
    
    if prompt := st.chat_input("Ask about your device... (e.g. 'why is my game lagging?')"):
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"): st.markdown(prompt)
        
        # 🔥 DIRECT ANSWER HANDLING
        if "is my game on" in prompt.lower() or "is the game running" in prompt.lower():
            current_pkg = get_foreground_pkg()
            if current_pkg == pkg:
                resp = f"✅ Yes, your game `{pkg}` is currently running in the foreground."
            else:
                resp = f"❌ No, your game is not in the foreground. Current app: `{current_pkg}`"
            st.chat_message("assistant").markdown(resp)
            st.session_state.chat_messages.append({"role": "assistant", "content": resp})
            st.stop()
        
        with st.chat_message("assistant"):
            # Step 1: Run tools locally (Deterministic MCP Engine)
            with st.spinner("🔧 Routing metrics query to local device..."):
                results, tools_called = route_query(prompt, pkg)
            
            st.info(f"🔧 Tools used: {', '.join(tools_called)}")

            response_full = ""
            if groq_api_key:
                # GROQ INTEGRATION (Explanation Layer)
                with st.spinner("⚡ Groq is analyzing your device metrics..."):
                    client = openai.OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1")
                    
                    # Step 2: Consolidated Prompt for LLM
                    # We merge the user's question and the tool results into one user message
                    # This ensures the LLM sees the data as context for the user's current request.
                    context_payload = {
                        "user_question": prompt,
                        "device_metrics": results,
                        "timestamp": datetime.now().isoformat()
                    }
                    
                    messages = [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Analyze these metrics for the user:\n\n{json.dumps(context_payload, indent=2)}"}
                    ]
                    
                    try:
                        response = client.chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=messages,
                            max_tokens=800,
                            temperature=0.2 # Lower temperature for better technical analysis
                        )
                        
                        response_full = response.choices[0].message.content
                    except Exception as e:
                        st.error(f"Groq API Error: {str(e)}")
                        response_full = format_ai_response(results, tools_called)
            else:
                response_full = format_ai_response(results, tools_called)
            
            # If for some reason LLM returns empty, use formatted response
            if not response_full or response_full.strip() == "":
                response_full = format_ai_response(results, tools_called)
                
            st.markdown(response_full)
            st.session_state.chat_messages.append({"role": "assistant", "content": response_full})