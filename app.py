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
    """Detect foreground package using multiple ADB methods for reliability."""
    # Method 1: dumpsys window mCurrentFocus (most reliable across Android versions)
    # Format: "mCurrentFocus=Window{hash u0 pkg/activity}"
    try:
        raw = subprocess.run(
            "adb shell dumpsys window", shell=True,
            capture_output=True, text=True, timeout=8
        ).stdout
        for line in raw.splitlines():
            if "mCurrentFocus" in line:
                # Match pattern: u0 <package>/ or just <package>/
                m = re.search(r"u0\s+([\w.]+)/", line)
                if m:
                    return m.group(1)
                # Some devices use format without u0
                m2 = re.search(r"\s([\w.]+)/[\w.]+\}", line)
                if m2:
                    return m2.group(1)
            if "mFocusedApp" in line:
                m = re.search(r"u0\s+([\w.]+)/", line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    
    # Method 2: Fallback to mResumedActivity
    raw2 = adb('"dumpsys activity | grep mResumedActivity"')
    match = re.search(r"u0 ([\w.]+)/", raw2)
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
            short_pkg = pkg.split('.')[-1]
            layer_df = tp.query(f"""
                SELECT 
                  COALESCE(layer_name, 'None') as layer_name,
                  COUNT(*) as c,
                  SUM(CASE WHEN layer_name LIKE '%{short_pkg}%' THEN 1000 ELSE 0 END) as priority_score
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
                ORDER BY priority_score DESC, c DESC
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
                    0 AS is_jank
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

    # ── Step 2B: Fallback — eglSwapBuffers (True Render Ticks) ──
    if df_results.empty:
        try:
            short_pkg = pkg.split('.')[-1]
            # Use eglSwapBuffers% which is 1:1 with pure engine render ticks, avoiding UI thread buffer bloat.
            query = f"""
            WITH app_slices AS (
              SELECT s.ts, s.dur
              FROM slice s
              JOIN thread_track tt ON s.track_id = tt.id
              JOIN thread t        ON tt.utid = t.utid
              JOIN process p       ON t.upid = p.upid
              WHERE p.name LIKE '%{short_pkg}%'
                AND (s.name LIKE 'eglSwapBuffers%' OR s.name LIKE 'vkQueuePresentKHR%')
                AND s.dur > 0
            ),
            min_ts AS (SELECT MIN(ts) as start_ts FROM app_slices),
            bucketed AS (
              SELECT
                CAST((ts-(SELECT start_ts FROM min_ts))/1e9 AS INT) AS time_s,
                ts/1e6  AS ts_ms,
                dur/1e6 AS dur_ms,
                0 AS is_jank
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
                    0 AS is_jank
                  FROM app_slices
                )
                SELECT time_s, ts_ms, dur_ms, is_jank,
                  COUNT(*) OVER (PARTITION BY time_s) AS fps_at_sec
                FROM bucketed ORDER BY ts_ms
                """
                df_results = tp.query(query2).as_pandas_dataframe()
                method_used = "atrace:Choreographer"
            else:
                method_used = "atrace:eglSwapBuffers"
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
    
    # Method 1: dumpsys gfxinfo (works for native UI and some engines)
    adb(f"dumpsys gfxinfo {package_name} reset")
    time.sleep(1.5)
    raw = adb(f"dumpsys gfxinfo {package_name}")
    janky = re.search(r"Janky frames: (\d+)", raw)
    total = re.search(r"Total frames rendered: (\d+)", raw)
    
    janky_frames = int(janky.group(1)) if janky else 0
    total_frames = int(total.group(1)) if total else 0
    
    # Method 2: Fallback to atrace if gfxinfo returns 0 (Unreal/Unity games)
    if total_frames == 0:
        try:
            # Capture 1.5s of gfx trace
            adb("atrace --async_start -c gfx view")
            time.sleep(1.5)
            trace_raw = adb("atrace --async_dump -c gfx")
            adb("atrace --async_stop")
            
            # Count render events indicating frames
            swap_count = trace_raw.count("eglSwapBuffers")
            queue_count = trace_raw.count("queueBuffer")
            do_frame_count = trace_raw.count("doFrame")
            
            # UE4/Unity use different render paths; queueBuffer is often 2-3x per frame, 
            # doFrame is UI thread, eglSwapBuffers is render thread.
            # A robust fallback estimate:
            if swap_count > 5:
                total_frames = swap_count
            elif do_frame_count > 5:
                total_frames = do_frame_count
            elif queue_count > 5:
                total_frames = int(queue_count / 2) # Usually 2-3 buffers queued per frame
            
            # Since this is over 1.5 seconds, we don't have exact jank %, but 
            # we at least have a valid frames rendered count for the interval.
        except Exception:
            pass

    return {
        "janky_frames": janky_frames,
        "total_frames_1_5s": total_frames,  # Provide context it's a 1.5s window
        "estimated_fps_hz": round(total_frames / 1.5, 1) if total_frames > 0 else 0
    }

def get_gpu_info():
    """Pull real-time GPU frequency and load from Android sysfs/dumpsys."""
    gpu_freq = adb('"cat /sys/class/kgsl/kgsl-3d0/gpuclk 2>/dev/null || cat /sys/kernel/gpu/gpu_clock 2>/dev/null || echo N/A"')
    gpu_busy = adb('"cat /sys/class/kgsl/kgsl-3d0/gpubusy 2>/dev/null || echo N/A"')
    gpu_governor = adb('"cat /sys/class/kgsl/kgsl-3d0/devfreq/governor 2>/dev/null || echo N/A"')
    return {
        "gpu_clock_hz": gpu_freq.strip(),
        "gpu_busy": gpu_busy.strip(),
        "gpu_governor": gpu_governor.strip(),
    }

def get_network_stats():
    """Get live network connectivity and data usage stats."""
    wifi_raw = adb('"dumpsys wifi | grep mWifiInfo"')
    net_stats = adb('"cat /proc/net/dev"')
    connectivity = adb('"dumpsys connectivity | grep NetworkAgentInfo"')
    # Parse active network type
    net_type = "Unknown"
    if "WIFI" in connectivity.upper():
        net_type = "WiFi"
    elif "MOBILE" in connectivity.upper() or "CELLULAR" in connectivity.upper():
        net_type = "Cellular"
    # Parse WiFi signal
    rssi_match = re.search(r"RSSI: (-?\d+)", wifi_raw)
    link_speed_match = re.search(r"Link speed: (\d+)", wifi_raw)
    return {
        "network_type": net_type,
        "wifi_rssi_dbm": int(rssi_match.group(1)) if rssi_match else None,
        "wifi_link_speed_mbps": int(link_speed_match.group(1)) if link_speed_match else None,
    }

def get_running_processes(package_name):
    """Get top running processes and check game foreground/background state."""
    top_raw = adb('"top -n 1 -m 10 -b"')
    proc_lines = []
    for line in top_raw.strip().splitlines():
        if '%' in line and not line.startswith('Mem') and not line.startswith('Tasks'):
            proc_lines.append(line.strip()[:120])
    fg_pkg = get_foreground_pkg()
    return {
        "foreground_app": fg_pkg,
        "game_is_foreground": fg_pkg == package_name if fg_pkg else False,
        "top_processes": proc_lines[:8],
    }

def get_display_info():
    """Get screen resolution, density, and refresh rate."""
    wm_size = adb('"wm size"')
    wm_density = adb('"wm density"')
    refresh_raw = adb('"dumpsys display | grep mRefreshRate"')
    brightness_raw = adb('"settings get system screen_brightness"')
    size_match = re.search(r"(\d+x\d+)", wm_size)
    density_match = re.search(r"(\d+)", wm_density)
    refresh_match = re.search(r"([\d.]+)", refresh_raw)
    return {
        "resolution": size_match.group(1) if size_match else "N/A",
        "density_dpi": int(density_match.group(1)) if density_match else None,
        "refresh_rate_hz": float(refresh_match.group(1)) if refresh_match else None,
        "brightness": brightness_raw.strip() if brightness_raw.strip() else "N/A",
    }

def get_disk_io():
    """Get disk I/O stats and available storage."""
    storage_raw = adb('"df /data | tail -1"')
    iostat_raw = adb('"cat /proc/diskstats | grep -E \"sda|mmcblk0\" | head -3"')
    parts = storage_raw.split() if storage_raw.strip() else []
    return {
        "storage_total": parts[1] if len(parts) > 1 else "N/A",
        "storage_used": parts[2] if len(parts) > 2 else "N/A",
        "storage_available": parts[3] if len(parts) > 3 else "N/A",
        "storage_use_pct": parts[4] if len(parts) > 4 else "N/A",
        "disk_io_raw": iostat_raw.strip()[:200] if iostat_raw.strip() else "N/A",
    }

def get_top_apps():
    """Get list of recently used / running applications."""
    raw = adb('"dumpsys activity recents | grep realActivity"')
    apps = re.findall(r"([\w.]+)/", raw)
    # De-duplicate while preserving order
    seen = set()
    unique_apps = []
    for a in apps:
        if a not in seen:
            seen.add(a)
            unique_apps.append(a)
    return {"recent_apps": unique_apps[:10]}

def get_full_realtime_snapshot(package_name):
    """MCP Master Tool: Pull ALL live data from Android in one shot for AI grounding."""
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "foreground_app": get_foreground_pkg(),
        "target_package": package_name,
        "battery": get_battery(),
        "cpu": get_cpu(package_name),
        "memory": get_memory(package_name),
        "thermals": get_thermals(),
        "display": get_display_info(),
        "gpu": get_gpu_info(),
        "network": get_network_stats(),
    }
    snapshot["game_is_foreground"] = snapshot["foreground_app"] == package_name
    return snapshot

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
    
    # Attach GPU and display for full context
    gpu = get_gpu_info()
    display = get_display_info()
    network = get_network_stats()
    
    return {
        "status": status,
        "foreground_app": current_pkg,
        "raw_snapshot": data,
        "fps_data": fps,
        "gpu": gpu,
        "display": display,
        "network": network,
        "diagnosed_issues": issues if issues else ["No major bottlenecks detected. Device is running smoothly."]
    }

SYSTEM_PROMPT = """
You are an expert Android Game Performance Analyst integrated into a real-time MCP (Model Context Protocol) diagnostic system.

You are directly connected to a live Android device via ADB. Every query you receive includes a REAL-TIME DEVICE SNAPSHOT
pulled from the actual device at the moment of the user's question. This is NOT simulated data — it is live telemetry.

Your responsibilities:
1. **Always reference the real-time data** — cite specific numbers (e.g., "Your CPU is at 72%", "Battery temp is 38.2°C").
2. **Identify bottlenecks** — thermal throttling, memory pressure, GPU frequency drops, high CPU, jank frames.
3. **Explain impact on gameplay** — connect metrics to user-visible effects (stutters, FPS drops, input lag).
4. **Give actionable fixes** — concrete steps like "lower resolution", "close background apps", "enable battery saver".
5. **Cross-correlate metrics** — e.g., high thermal + dropping GPU clock = thermal throttle causing FPS drops.
6. **Report device state honestly** — if the game is not in the foreground, say so. If data is unavailable, note it.

IMPORTANT: You are talking directly to the user who is playing a game on their Android phone.
Be CONCISE, TECHNICAL, and always ground your analysis in the ACTUAL DATA provided.
Do NOT make up data. Only analyze what the device snapshot provides.
"""


AVAILABLE_TOOLS = {
    "get_cpu_usage":       {"function": lambda p: get_cpu_mcp(p),           "description": "Get current CPU % load for the game.",            "keywords": ["cpu", "load", "processor"]},
    "get_memory_usage":    {"function": lambda p: get_memory_mcp(p),        "description": "Get RAM/Memory PSS usage.",                       "keywords": ["ram", "memory", "leak", "oom"]},
    "get_fps":             {"function": lambda p: get_fps_mcp(p),           "description": "Get total frames and janky stutters.",            "keywords": ["fps", "jank", "stutter", "lag", "frames", "smooth", "framerate"]},
    "get_thermal":         {"function": lambda _: get_thermal_mcp(),        "description": "Get max thermal zone temperature.",               "keywords": ["temp", "thermal", "overheating", "hot", "throttle", "heat"]},
    "get_battery":         {"function": lambda _: get_battery_mcp(),        "description": "Get battery %, temperature, voltage.",            "keywords": ["battery", "power", "charge", "drain"]},
    "get_gpu":             {"function": lambda _: get_gpu_info(),           "description": "Get GPU clock, busy %, and governor.",            "keywords": ["gpu", "graphics", "render", "adreno", "mali"]},
    "get_network":         {"function": lambda _: get_network_stats(),      "description": "Get network type, WiFi signal, link speed.",      "keywords": ["network", "wifi", "ping", "signal", "internet", "latency", "connection"]},
    "get_display":         {"function": lambda _: get_display_info(),       "description": "Get screen resolution, density, refresh rate.",   "keywords": ["display", "screen", "resolution", "refresh", "brightness"]},
    "get_processes":       {"function": lambda p: get_running_processes(p), "description": "Get top processes and foreground app.",           "keywords": ["process", "running", "foreground", "background", "app", "kill"]},
    "get_disk":            {"function": lambda _: get_disk_io(),            "description": "Get storage usage and disk I/O.",                "keywords": ["disk", "storage", "space", "io"]},
    "get_top_apps":        {"function": lambda _: get_top_apps(),           "description": "Get recently used applications.",                "keywords": ["recent", "apps", "open", "switch"]},
    "analyze_performance": {"function": lambda p: analyze_performance_engine(p), "description": "Run the AI Lag Diagnosis Engine to detect bottlenecks.", "keywords": ["diagnose", "analyze", "why", "lagging", "issue", "problem", "slow", "bad", "performance", "check", "report", "status"]}
}

def route_query(query, pkg):
    query_lower = query.lower()
    matched_tools = [name for name, info in AVAILABLE_TOOLS.items() if any(kw in query_lower for kw in info["keywords"])]
    if not matched_tools: matched_tools = ["analyze_performance"]
    
    results = {}
    for tool_name in matched_tools:
        results[tool_name] = AVAILABLE_TOOLS[tool_name]["function"](pkg)
    return results, matched_tools

def format_ai_response(results, tools_called, live_snapshot=None):
    """Format a rich fallback response when Groq is unavailable."""
    analysis = results.get("analyze_performance", {})
    status = analysis.get("status", "unknown")
    foreground = analysis.get("foreground_app", "unknown")
    
    parts = [
        "🤖 **Android Performance Analyst — Live Device Report**\n",
        f"📱 Foreground App: `{foreground}`",
        f"🎮 Game Status: **{status.upper()}**\n",
    ]
    
    # Show live snapshot summary if available
    if live_snapshot:
        batt = live_snapshot.get('battery', {})
        cpu = live_snapshot.get('cpu', {})
        mem = live_snapshot.get('memory', {})
        therm = live_snapshot.get('thermals', {})
        disp = live_snapshot.get('display', {})
        gpu = live_snapshot.get('gpu', {})
        net = live_snapshot.get('network', {})
        parts.append("**📊 Real-Time Device Snapshot:**")
        parts.append(f"- 🔋 Battery: {batt.get('battery_pct', 'N/A')}% | Temp: {batt.get('battery_temp', 'N/A')}°C")
        parts.append(f"- ⚡ CPU: {cpu.get('cpu_pct', 0):.1f}% | GPU Clock: {gpu.get('gpu_clock_hz', 'N/A')}")
        parts.append(f"- 💾 RAM (PSS): {mem.get('mem_pss_mb', 0):.0f} MB")
        parts.append(f"- 🌡️ Max Thermal: {therm.get('max_thermal_c', 'N/A')}°C")
        parts.append(f"- 🖥️ Display: {disp.get('resolution', 'N/A')} @ {disp.get('refresh_rate_hz', 'N/A')} Hz")
        parts.append(f"- 🌐 Network: {net.get('network_type', 'N/A')} | Signal: {net.get('wifi_rssi_dbm', 'N/A')} dBm\n")
    
    parts.append(f"*Tools executed:* `{'`, `'.join(tools_called)}`\n")
    parts.append("---")
    parts.append("*Raw Diagnostic Data:*")
    parts.append(f"```json\n{json.dumps(results, indent=2)}\n```")
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
    target_fps = st.selectbox("🎯 Exact Game FPS (Target/Vsync)", [30, 45, 60, 90, 120, 144], index=2, help="Change this instantly to evaluate exact jank limits based on the game's actual FPS max.")
    st.session_state.target_fps = target_fps
    
    # Checkbox to override real_fps strictly
    override_fps = st.checkbox("Force Exact FPS (Override metrics if detection fails for this game)")
    st.session_state.override_fps = override_fps
    
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
            fig2 = go.Figure()
            if "cpu_pct" in adb_df.columns:
                fig2.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["cpu_pct"], name="CPU %", fill="tozeroy"))
            if "mem_pss_mb" in adb_df.columns:
                fig2.add_trace(go.Scatter(x=adb_df["time_s"], y=adb_df["mem_pss_mb"], name="RAM PSS", yaxis="y2"))
            fig2.update_layout(yaxis2=dict(overlaying="y", side="right"), height=400)
            st.plotly_chart(fig2, use_container_width=True)

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