import os
import json
from datetime import datetime
from adb_utils import (
    adb, get_battery, get_memory, get_cpu, get_thermals, 
    get_foreground_pkg, collect_snapshot, get_fps_stats, 
    get_gpu_info, get_network_stats, get_running_processes, 
    get_display_info, get_disk_io, get_top_apps
)

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

def get_full_realtime_snapshot(package_name):
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
    
    fps = get_fps_stats(package_name)
    if status == "running" and fps.get("janky_frames", 0) > 5:
        issues.append("High frame drops (jank detected)")
    
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
    "get_cpu_usage":       {"function": lambda p: get_cpu(p),               "description": "Get current CPU % load for the game.",            "keywords": ["cpu", "load", "processor"]},
    "get_memory_usage":    {"function": lambda p: get_memory(p),            "description": "Get RAM/Memory PSS usage.",                       "keywords": ["ram", "memory", "leak", "oom"]},
    "get_fps":             {"function": lambda p: get_fps_stats(p),         "description": "Get total frames and janky stutters.",            "keywords": ["fps", "jank", "stutter", "lag", "frames", "smooth", "framerate"]},
    "get_thermal":         {"function": lambda _: get_thermals(),           "description": "Get max thermal zone temperature.",               "keywords": ["temp", "thermal", "overheating", "hot", "throttle", "heat"]},
    "get_battery":         {"function": lambda _: get_battery(),            "description": "Get battery %, temperature, voltage.",            "keywords": ["battery", "power", "charge", "drain"]},
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
    analysis = results.get("analyze_performance", {})
    status = analysis.get("status", "unknown")
    foreground = analysis.get("foreground_app", "unknown")
    
    parts = [
        "🤖 **Android Performance Analyst — Live Device Report**\n",
        f"📱 Foreground App: `{foreground}`",
        f"🎮 Game Status: **{status.upper()}**\n",
    ]
    
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
