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

# ─── Internal Modules ───────────────────────────────────────────
from adb_utils import adb, get_foreground_pkg, collect_snapshot, get_fps_stats
from perfetto_utils import run_perfetto_trace, parse_perfetto_trace
from ai_agent import get_full_realtime_snapshot, route_query, format_ai_response, SYSTEM_PROMPT

# ═══════════════════════════════════════════════════════════════
# STREAMLIT UI
# ═══════════════════════════════════════════════════════════════
st.set_page_config(page_title="Android Game Profiler", layout="wide")
st.title("🎮 Android Game Profiler")
st.caption("LnT × Meta Internship Tool | Live Monitor + Perfetto Session Recorder + AI Chat")

defaults = {"running": False, "data": [], "pkg": "", "log_path": None, "live_history": [], "chat_messages": [], "perfetto_data": None}
for key, default in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = default

with st.sidebar:
    st.header("⚙️ Configuration")
    mode = st.radio("📌 Select Mode", ["📡 Real-Time Monitor", "🎬 Perfetto Session Recorder", "🤖 AI Chat (MCP)"], index=0)
    st.divider()
    
    # Auto-detect foreground app button
    if st.button("🎯 Auto-Detect Running Game", use_container_width=True):
        detected_pkg = get_foreground_pkg()
        if detected_pkg:
            st.session_state.pkg = detected_pkg
            st.success(f"Detected: {detected_pkg}")
        else:
            st.error("Could not detect foreground app")
            
    pkg = st.text_input("Package name", value=st.session_state.pkg)
    st.session_state.pkg = pkg
    target_fps = 60
    override_fps = False
    duration = 10
    
    if mode == "🎬 Perfetto Session Recorder":
        target_fps = st.selectbox("🎯 Exact Game FPS (Target/Vsync)", [30, 45, 60, 90, 120, 144], index=2, help="Change this instantly to evaluate exact jank limits based on the game's actual FPS max.")
        override_fps = st.checkbox("Force Exact FPS (Override metrics if detection fails for this game)")
        duration = st.slider("Recording duration (sec)", 5, 120, 10, help="For Perfetto traces, 10-20s is recommended to avoid gigantic trace files.")
        
    st.session_state.target_fps = target_fps
    st.session_state.override_fps = override_fps
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
        
        # --- Foreground App Badge ---
        fg_pkg = get_foreground_pkg()
        if fg_pkg == pkg:
            st.success(f"🟢 **ACTIVE:** [{pkg}]")
        else:
            st.error(f"🔴 **MINIMIZED/BACKGROUNDED:** [{fg_pkg or 'Unknown App'}]")

        # 1 & 2. ALERTS and METRICS WITH DELTAS
        prev_live = st.session_state.live_history[-1] if len(st.session_state.live_history) > 0 else live
        
        def safe_diff(cur, prev):
            try: return float(cur) - float(prev)
            except: return 0.0
            
        dbatt = safe_diff(live.get('battery_pct'), prev_live.get('battery_pct'))
        dtemp = safe_diff(live.get('battery_temp'), prev_live.get('battery_temp'))
        dcpu = safe_diff(live.get('cpu_pct'), prev_live.get('cpu_pct'))
        dmem = safe_diff(live.get('mem_pss_mb'), prev_live.get('mem_pss_mb'))

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🔋 Battery", f"{live.get('battery_pct', 'N/A')}%", delta=f"{dbatt:+.1f}%" if dbatt else None)
        c2.metric("🌡️ Temp", f"{live.get('battery_temp', 'N/A')}°C", delta=f"{dtemp:+.1f}°C" if dtemp else None, delta_color="inverse")
        c3.metric("⚡ Peak CPU", f"{live.get('cpu_pct', 0.0):.1f}%", delta=f"{dcpu:+.1f}%" if dcpu else None, delta_color="inverse")
        
        total_ram = live.get("total_ram_mb", 0)
        pss = live.get("mem_pss_mb", 0)
        
        if pss and total_ram:
            c4.metric("💾 RAM (PSS)", f"{pss:.0f} MB / {(total_ram/1024):.1f} GB")
            live["mem_pct"] = (pss / total_ram) * 100
        elif pss:
            c4.metric("💾 RAM (PSS)", f"{pss:.0f} MB")
            live["mem_pct"] = 0
        else:
            c4.metric("💾 RAM (PSS)", "N/A")
            live["mem_pct"] = 0
            
        # --- Convert WiFi RSSI to 0-100% Signal Quality ---
        dbm = live.get("wifi_rssi_dbm")
        if dbm is not None:
            # Typical representation: <= -100 dBm is 0%, >= -50 dBm is 100%
            signal_pct = 2 * (dbm + 100)
            live["wifi_signal_pct"] = max(0, min(100, signal_pct))
        else:
            live["wifi_signal_pct"] = 0
        
        _cpu_val = float(live.get('cpu_pct', 0)) if live.get('cpu_pct') else 0.0
        _temp_val = float(live.get('battery_temp', 0)) if live.get('battery_temp') else 0.0
        
        # High CPU on multi-core can go up to 800%. Warning at 400% (4 cores maxed out).
        if _cpu_val > 400:
            st.warning(f"⚠️ **HIGH CPU:** Usage is heavily multi-threading ({_cpu_val}%). Keep an eye on battery drain.")
        if _temp_val > 42:
            st.error(f"⚠️ **CRITICAL:** Temperature exceeds 42°C ({_temp_val}°C)! Thermal throttling is highly likely.")
        elif _temp_val > 39:
            st.warning(f"⚠️ **WARNING:** Device is getting hot (> 39°C). Keep an eye on temperature.")

        live["time"] = datetime.now().strftime("%M:%S")
        st.session_state.live_history.append(live)
        if len(st.session_state.live_history) > 60:
            st.session_state.live_history = st.session_state.live_history[-60:]
        
        if len(st.session_state.live_history) > 1:
            hist_df = pd.DataFrame(st.session_state.live_history)
            
            # --- Graph 1: CPU ---
            fig_cpu = go.Figure()
            if "cpu_pct" in hist_df.columns:
                fig_cpu.add_trace(go.Scatter(x=hist_df["time"], y=hist_df["cpu_pct"], name="CPU %", line={"color": "#3498db", "width": 2}, fill="tozeroy"))
            # Auto-scale Y-axis for CPU because 8-core devices can hit 800%
            fig_cpu.update_layout(title="CPU Utilization ", height=250, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(rangemode="tozero"))

            # --- Graph 2: Temperature ---
            fig_temp = go.Figure()
            if "battery_temp" in hist_df.columns:
                fig_temp.add_trace(go.Scatter(x=hist_df["time"], y=hist_df["battery_temp"], name="Temp °C", line={"color": "#e74c3c", "width": 2}, fill="tozeroy"))
            fig_temp.update_layout(title="Device Temperature (°C)", height=250, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(range=[20, 60]))
            fig_temp.add_hline(y=42, line_dash="dash", line_color="red", annotation_text="Throttling Limit: 42°C")

            # --- Graph 3: RAM (PSS) ---
            fig_mem = go.Figure()
            if "mem_pct" in hist_df.columns:
                fig_mem.add_trace(go.Scatter(x=hist_df["time"], y=hist_df["mem_pct"], name="RAM %", line={"color": "#9b59b6", "width": 2}, fill="tozeroy"))
            fig_mem.update_layout(title="RAM Leak Monitor (% of Total)", height=250, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(range=[0, 100]))

            # --- Graph 4: Network (Signal %) ---
            fig_net = go.Figure()
            if "wifi_signal_pct" in hist_df.columns:
                fig_net.add_trace(go.Scatter(x=hist_df["time"], y=hist_df["wifi_signal_pct"], name="WiFi Quality %", line={"color": "#f1c40f", "width": 2}, fill="tozeroy"))
            fig_net.update_layout(title="Network Stability (Signal Quality %)", height=250, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(range=[0, 100]))

            # --- Render in Grid Layout ---
            row1_col1, row1_col2 = st.columns(2)
            with row1_col1:
                st.plotly_chart(fig_cpu, use_container_width=True)
            with row1_col2:
                st.plotly_chart(fig_temp, use_container_width=True)
            
            row2_col1, row2_col2 = st.columns(2)
            with row2_col1:
                st.plotly_chart(fig_mem, use_container_width=True)
            with row2_col2:
                st.plotly_chart(fig_net, use_container_width=True)
        
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
        
        # 1. Push Perfetto config to device
        with open("perfetto_config.pbtx", "w") as f:
            config = f'''buffers {{ size_kb: 65536 fill_policy: RING_BUFFER }} write_into_file: true file_write_period_ms: 2500 data_sources {{ config {{ name: "android.surfaceflinger.frametimeline" }} }} data_sources {{ config {{ name: "android.surfaceflinger" }} }} data_sources {{ config {{ name: "linux.ftrace" ftrace_config {{ ftrace_events: "ftrace/print" ftrace_events: "power/cpu_frequency" ftrace_events: "power/cpu_idle" atrace_categories: "gfx" atrace_categories: "view" atrace_categories: "wm" atrace_categories: "sched" atrace_categories: "freq" atrace_categories: "rs" atrace_categories: "am" atrace_apps: "{pkg}" atrace_apps: "*" }} }} }} data_sources {{ config {{ name: "linux.process_stats" process_stats_config {{ scan_all_processes_on_start: true proc_stats_poll_ms: 1000 }} }} }} duration_ms: {duration * 1000}'''
            f.write(config)
        subprocess.run("adb push perfetto_config.pbtx /data/local/tmp/perfetto_config.pbtx", shell=True)
        adb(f"dumpsys gfxinfo {pkg} reset")

        # 2. Live countdown UI — replaces static spinner
        _rec_header = st.empty()
        _rec_progress = st.empty()
        _rec_header.info(f"🎥 **Recording system trace for {duration}s** — Keep the game **actively playing** on the device!")

        start_wall = time.time()
        perfetto_proc = subprocess.Popen("adb shell \"cat /data/local/tmp/perfetto_config.pbtx | perfetto --txt -c - -o /data/misc/perfetto-traces/trace.perfetto-trace\"", shell=True)

        # 3. Main thread polling + live progress bar
        adb_results = []
        end_time_limit = time.time() + duration
        while time.time() < end_time_limit:
            elapsed = time.time() - start_wall
            remaining = max(0, int(duration - elapsed))
            _rec_progress.progress(
                min(elapsed / duration, 1.0),
                text=f"⏱️ {int(elapsed)}s elapsed — ⏳ {remaining}s remaining | 📊 {len(adb_results)} snapshots captured"
            )
            snap = collect_snapshot(pkg, datetime.now().isoformat())
            adb_results.append(snap)
            time.sleep(1.0)

        _rec_header.success("✅ Recording complete! Pulling trace from device...")
        _rec_progress.empty()

        # 4. Teardown & pull trace
        perfetto_proc.wait()
        end_wall = time.time()
        st.session_state["session_duration"] = end_wall - start_wall
        st.session_state["gfx_summary"] = get_fps_stats(pkg)
        time.sleep(2.0)
        trace_file = f"trace_{pkg}_{int(time.time())}.perfetto-trace"
        subprocess.run(f"adb pull /data/misc/perfetto-traces/trace.perfetto-trace {trace_file}", shell=True)
        st.session_state["trace_file"] = trace_file

        st.session_state.running = False
        st.session_state.data = adb_results
        
        with st.spinner("🧠 Booting Perfetto SQL Trace Processor & Dissecting Trace..."):
            perfetto_df, method_used = parse_perfetto_trace(trace_file, pkg, target_fps)
            st.session_state["trace_method"] = method_used
            
            if not perfetto_df.empty:
                st.session_state.perfetto_data = perfetto_df.to_dict('records')
                st.success(f"✅ Perfetto processing complete! Method: `{method_used}`")
            else:
                st.session_state.perfetto_data = "EMPTY"
                if "init_error" in method_used:
                    st.error(f"Trace processing failed: {method_used}")
                else:
                    st.warning("⚠️ No frame data found. Try: keep the game actively playing during recording.")

    # ─── Dashboard Render ───────────────────
    if st.session_state.data and st.session_state.perfetto_data:
        adb_df = pd.DataFrame(st.session_state.data)
        adb_df["time_s"] = range(len(adb_df))
        
        st.divider()
        st.subheader("📊 Session Summary")
        m1, m2, m3, m4, m5, m6 = st.columns(6)

        # Fix 2: Sub-integer battery drain using voltage delta for precision
        if not adb_df['battery_pct'].isnull().all():
            int_drain = float(adb_df['battery_pct'].iloc[0] - adb_df['battery_pct'].iloc[-1])
            if 'battery_volt' in adb_df.columns and not adb_df['battery_volt'].isnull().all():
                volt_first = adb_df['battery_volt'].dropna().iloc[0]
                volt_last  = adb_df['battery_volt'].dropna().iloc[-1]
                volt_drop  = volt_first - volt_last
                # LiPo range: 4.2V (100%) → 3.4V (0%) = 0.8V total → 0.008V per 1%
                volt_drain_pct = volt_drop / 0.008
                precise_drain = max(int_drain, round(volt_drain_pct, 4))
            else:
                precise_drain = int_drain
            m1.metric("🔋 Batt Drain", f"{precise_drain:.4f}%")
        else:
            m1.metric("🔋 Batt Drain", "N/A")

        m2.metric("🌡️ Max Temp", f"{adb_df['battery_temp'].max():.1f}°C" if not adb_df['battery_temp'].isnull().all() else "N/A")
        m3.metric("⚡ Peak CPU", f"{adb_df['cpu_pct'].max():.1f}%" if not adb_df['cpu_pct'].isnull().all() else "N/A")

        # Fix 3: Avg RAM with total device context
        _avg_pss     = adb_df['mem_pss_mb'].mean() if 'mem_pss_mb' in adb_df.columns and not adb_df['mem_pss_mb'].isnull().all() else None
        _total_ram_s = adb_df['total_ram_mb'].mean() if 'total_ram_mb' in adb_df.columns and not adb_df['total_ram_mb'].isnull().all() else None
        if _avg_pss and _total_ram_s:
            _ram_pct_s = (_avg_pss / _total_ram_s) * 100
            m4.metric("💾 Avg RAM", f"{_avg_pss:.0f} MB / {(_total_ram_s / 1024):.1f} GB", delta=f"{_ram_pct_s:.1f}% of total", delta_color="off")
        elif _avg_pss:
            m4.metric("💾 Avg RAM", f"{_avg_pss:.0f} MB")
        else:
            m4.metric("💾 Avg RAM", "N/A")

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

        if st.session_state.get("override_fps"):
            real_fps = float(st.session_state.target_fps)

        # NO hardcoded clamp — respect whatever the game's actual FPS cap is
        # (30, 60, 90, 120 Hz are all valid)

        fps_limit_ms = 1000.0 / st.session_state.target_fps

        # Jank: prefer Perfetto data, fall back to gfxinfo
        if st.session_state.perfetto_data != "EMPTY":
            pdf = pd.DataFrame(st.session_state.perfetto_data)
            pdf["is_jank"] = (pdf["dur_ms"] > fps_limit_ms).astype(int)
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
        # Fix 4: Jank % with color-coded health verdict
        if jank_pct < 2:
            _jank_verdict, _jank_color = "🟢 Excellent", "normal"
        elif jank_pct < 5:
            _jank_verdict, _jank_color = "🟡 Acceptable", "off"
        elif jank_pct < 10:
            _jank_verdict, _jank_color = "🟠 Warning", "inverse"
        else:
            _jank_verdict, _jank_color = "🔴 Critical", "inverse"
        m6.metric("⚠️ Jank %", f"{jank_pct:.1f}%", delta=_jank_verdict, delta_color=_jank_color)
        
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
                fps_limit_ms = 1000.0 / st.session_state.target_fps
                pdf["is_jank"] = (pdf["dur_ms"] > fps_limit_ms).astype(int)
                
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
                fps_limit_ms = 1000.0 / st.session_state.target_fps
                fig_lat = px.line(pdf, x="ts_ms", y="dur_ms", title=f"Frame Latency (ms) - Target < {fps_limit_ms:.1f}ms for {st.session_state.target_fps}FPS", labels={"dur_ms": "Latency (ms)", "ts_ms": "Time (ms)"})
                fig_lat.add_hline(y=fps_limit_ms, line_dash="dash", line_color="red", annotation_text=f"{st.session_state.target_fps} FPS Limit")
                fig_lat.update_layout(height=400)
                st.plotly_chart(fig_lat, use_container_width=True)
            else:
                st.info("No latency data available for this trace.")
                
        with t3:
            # Fix 6: Split into clearly labelled side-by-side charts — no dual-Y confusion
            hw_c1, hw_c2 = st.columns(2)
            with hw_c1:
                fig_cpu_s = go.Figure()
                if "cpu_pct" in adb_df.columns:
                    fig_cpu_s.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["cpu_pct"], name="CPU %", line={"color": "#3498db", "width": 2}, fill="tozeroy"))
                fig_cpu_s.update_layout(title="CPU Utilization (%)", height=350, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(rangemode="tozero", title="CPU %"))
                st.plotly_chart(fig_cpu_s, use_container_width=True)
            with hw_c2:
                fig_ram_s = go.Figure()
                if "mem_pss_mb" in adb_df.columns and "total_ram_mb" in adb_df.columns:
                    adb_df["mem_pct_s"] = (adb_df["mem_pss_mb"] / adb_df["total_ram_mb"].replace(0, float("nan"))) * 100
                    fig_ram_s.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["mem_pct_s"], name="RAM %", line={"color": "#9b59b6", "width": 2}, fill="tozeroy"))
                    fig_ram_s.update_layout(title="RAM Usage (% of Total)", height=350, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(range=[0, 100], title="RAM %"))
                elif "mem_pss_mb" in adb_df.columns:
                    fig_ram_s.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["mem_pss_mb"], name="RAM (MB)", line={"color": "#9b59b6", "width": 2}, fill="tozeroy"))
                    fig_ram_s.update_layout(title="RAM Usage (MB)", height=350, margin=dict(t=40, b=10, l=10, r=10), yaxis=dict(rangemode="tozero", title="MB"))
                st.plotly_chart(fig_ram_s, use_container_width=True)

# ══════════════════════════════════════════════════════════════
# MODE 3: AI CHAT (MCP) — Real-Time Android Data Communication
# ══════════════════════════════════════════════════════════════
elif mode == "🤖 AI Chat (MCP)":
    st.subheader("🤖 MCP AI Chat — Live Android Data Link")
    st.caption("Every response is grounded in real-time device telemetry pulled directly via ADB.")
    
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    
    # ── Live Device Status Strip ──
    with st.spinner("📡 Pulling live device snapshot..."):
        _live = get_full_realtime_snapshot(pkg)
    
    _batt = _live.get('battery', {})
    _cpu = _live.get('cpu', {})
    _mem = _live.get('memory', {})
    _therm = _live.get('thermals', {})
    _disp = _live.get('display', {})
    _fg = _live.get('foreground_app', 'N/A')
    _game_fg = _live.get('game_is_foreground', False)
    
    ls1, ls2, ls3, ls4, ls5 = st.columns(5)
    ls1.metric("🔋 Battery", f"{_batt.get('battery_pct', 'N/A')}%")
    ls2.metric("🌡️ Temp", f"{_batt.get('battery_temp', 'N/A')}°C")
    ls3.metric("⚡ CPU", f"{_cpu.get('cpu_pct', 0):.1f}%")
    ls4.metric("💾 RAM", f"{_mem.get('mem_pss_mb', 0):.0f} MB" if _mem.get('mem_pss_mb') else "N/A")
    ls5.metric("📱 Game FG", "✅ Yes" if _game_fg else f"❌ {_fg}")
    
    if groq_api_key:
        st.success("✅ Groq AI connected | Real-time MCP data link active.")
    else:
        st.warning("⚠️ No GROQ_API_KEY. Using local analysis engine (all data still live from device).")
    
    st.divider()
    
    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]): st.markdown(msg["content"])
    
    if prompt := st.chat_input("Ask anything about your device... (e.g. 'why is my game lagging?', 'show GPU stats', 'network quality?')"):
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"): st.markdown(prompt)
        
        # 🔥 DIRECT ANSWER HANDLING (Instant responses)
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
            # Step 1: Pull fresh real-time snapshot (always — this grounds every response)
            with st.spinner("📡 Pulling fresh real-time data from Android device..."):
                live_snapshot = get_full_realtime_snapshot(pkg)
            
            # Step 2: Route to specific MCP tools based on query keywords
            with st.spinner("🔧 Executing MCP tool queries on device..."):
                results, tools_called = route_query(prompt, pkg)
            
            st.info(f"📡 **Live Data Link** | 🔧 Tools: {', '.join(tools_called)} | ⏱️ Snapshot: {live_snapshot['timestamp']}")

            response_full = ""
            if groq_api_key:
                with st.spinner("⚡ Groq AI analyzing real-time device data..."):
                    client = openai.OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1")
                    
                    # Consolidated context: user query + tool results + full live snapshot
                    context_payload = {
                        "user_question": prompt,
                        "target_package": pkg,
                        "realtime_device_snapshot": live_snapshot,
                        "tool_results": results,
                        "tools_executed": tools_called,
                        "query_timestamp": datetime.now().isoformat()
                    }
                    
                    messages = [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": (
                            f"User asks: \"{prompt}\"\n\n"
                            f"Below is the REAL-TIME data pulled from their Android device right now.\n"
                            f"Analyze this data to answer their question. Always cite specific numbers from the snapshot.\n\n"
                            f"{json.dumps(context_payload, indent=2)}"
                        )}
                    ]
                    
                    try:
                        response = client.chat.completions.create(
                            model="llama-3.3-70b-versatile",
                            messages=messages,
                            max_tokens=1000,
                            temperature=0.2
                        )
                        response_full = response.choices[0].message.content
                    except Exception as e:
                        st.error(f"Groq API Error: {str(e)}")
                        response_full = format_ai_response(results, tools_called, live_snapshot)
            else:
                response_full = format_ai_response(results, tools_called, live_snapshot)
            
            if not response_full or response_full.strip() == "":
                response_full = format_ai_response(results, tools_called, live_snapshot)
                
            st.markdown(response_full)
            st.session_state.chat_messages.append({"role": "assistant", "content": response_full})