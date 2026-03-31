# mcp_server.py — MCP Server for Android Game Profiler
# Exposes all ADB diagnostic tools as formal MCP Tools via FastMCP
# Run standalone: python mcp_server.py
# Or connect from Streamlit via stdio transport

import json
from mcp.server.fastmcp import FastMCP

# Import all ADB tool functions from existing modules
from adb_utils import (
    adb, get_battery, get_memory, get_cpu, get_thermals,
    get_foreground_pkg, collect_snapshot, get_fps_stats,
    get_gpu_info, get_network_stats, get_running_processes,
    get_display_info, get_disk_io, get_top_apps, get_total_ram_mb, batch_adb_snapshot
)
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# CREATE MCP SERVER
# ═══════════════════════════════════════════════════════════════
mcp = FastMCP(
    "android-game-profiler",
    instructions="""You are connected to a live Android device via ADB through this MCP server.
Use the available tools to pull real-time hardware telemetry (CPU, GPU, RAM, thermals, FPS, network, battery) 
from the device. Always call the relevant tools before answering — do NOT guess or make up data.
When diagnosing performance issues, call multiple tools to cross-correlate metrics."""
)

# ═══════════════════════════════════════════════════════════════
# MCP TOOLS — Each wraps an existing ADB function
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
def get_cpu_usage(package_name: str) -> str:
    """Get the current CPU usage percentage for a specific Android app package.
    Use this when the user asks about CPU load, processor usage, or performance bottlenecks."""
    result = get_cpu(package_name)
    return json.dumps(result)

@mcp.tool()
def get_memory_usage(package_name: str) -> str:
    """Get RAM/Memory usage (PSS and RSS in MB) for a specific Android app package.
    Use this when the user asks about memory, RAM, memory leaks, or OOM issues."""
    result = get_memory(package_name)
    total_ram = get_total_ram_mb()
    result["total_ram_mb"] = total_ram
    if result.get("mem_pss_mb") and total_ram:
        result["mem_pct"] = round((result["mem_pss_mb"] / total_ram) * 100, 1)
    return json.dumps(result)

@mcp.tool()
def get_fps(package_name: str) -> str:
    """Get frame rendering stats including total frames, janky frames, and estimated FPS.
    Use this when the user asks about FPS, frame rate, jank, stutters, smoothness, or lag.
    Note: This takes ~6 to 12 seconds to measure as it captures a 5-second moving average window.
    Uses gfxinfo first, then falls back to atrace for Unity/Unreal games."""
    result = get_fps_stats(package_name)
    if result.get("total_frames_measured", 0) == 0:
        result["note"] = "Both gfxinfo and atrace returned 0 frames — the game may not be actively rendering. Use Perfetto Session Recorder for deep FPS analysis."
    else:
        result["note"] = ""
    return json.dumps(result)

@mcp.tool()
def get_thermal_stats() -> str:
    """Get the maximum thermal zone temperature from the device's thermal sensors.
    Use this when the user asks about temperature, overheating, thermal throttling, or if the device is hot."""
    result = get_thermals()
    return json.dumps(result)

@mcp.tool()
def get_battery_stats() -> str:
    """Get battery percentage, temperature (°C), and voltage (V) from the device.
    Use this when the user asks about battery, power, charging, or drain."""
    result = get_battery()
    return json.dumps(result)

@mcp.tool()
def get_gpu_stats() -> str:
    """Get real-time GPU frequency (clock), GPU busy percentage, and governor mode.
    Use this when the user asks about GPU, graphics, rendering performance, Adreno, or Mali."""
    result = get_gpu_info()
    return json.dumps(result)

@mcp.tool()
def get_network_info() -> str:
    """Get network connectivity type (WiFi/Cellular), WiFi signal strength (RSSI in dBm), 
    link speed, and real-time TX/RX throughput in kbps.
    Use this when the user asks about network, WiFi, ping, signal, internet, latency, or connection quality."""
    result = get_network_stats()
    return json.dumps(result)

@mcp.tool()
def get_screen_info() -> str:
    """Get screen resolution, pixel density (DPI), refresh rate (fps), and brightness level.
    Use this when the user asks about display, screen, resolution, refresh rate, or brightness."""
    result = get_display_info()
    return json.dumps(result)

@mcp.tool()
def get_processes(package_name: str) -> str:
    """Get the top running processes and check whether the target game is in the foreground.
    Use this when the user asks about running apps, foreground/background state, or process management."""
    result = get_running_processes(package_name)
    return json.dumps(result)

@mcp.tool()
def get_disk_stats() -> str:
    """Get storage usage (total, used, available, percentage) and raw disk I/O stats.
    Use this when the user asks about storage, disk space, or I/O performance."""
    result = get_disk_io()
    return json.dumps(result)

@mcp.tool()
def get_recent_apps() -> str:
    """Get a list of recently used applications on the device.
    Use this when the user asks about recent apps, open apps, or app switching."""
    result = get_top_apps()
    return json.dumps(result)

@mcp.tool()
def detect_foreground_app() -> str:
    """Detect which app is currently in the foreground on the Android device.
    Use this when the user asks if their game is running, or what app is currently active."""
    pkg = get_foreground_pkg()
    return json.dumps({"foreground_package": pkg or "unknown"})

@mcp.tool()
def get_full_snapshot(package_name: str) -> str:
    """Get a complete device snapshot with ALL metrics at once via an optimized batched call.
    Use this for comprehensive analysis or when the user asks a broad question like 
    'how is my device doing?' or 'give me a full report'."""
    data = batch_adb_snapshot(package_name)
    return json.dumps(data)

@mcp.tool()
def analyze_game_performance(package_name: str) -> str:
    """Run the full AI Lag Diagnosis Engine. Checks CPU, memory, thermals, FPS, GPU, network,
    and cross-correlates metrics to detect bottlenecks like thermal throttling, memory pressure,
    high jank, etc. Use this when the user asks 'why is my game lagging?', 'diagnose performance',
    'what's wrong?', or any broad performance question."""
    data = collect_snapshot(package_name, datetime.now().isoformat())
    current_pkg = get_foreground_pkg()
    issues = []
    status = "running"

    if current_pkg != package_name:
        status = "not_running_or_background"
        issues.append(f"Game is not in foreground (Current: {current_pkg})")

    if data.get("cpu_pct", 0) > 400:
        issues.append(f"Very high CPU usage ({data['cpu_pct']:.1f}%) — heavy multi-threading")
    elif data.get("cpu_pct", 0) > 80:
        issues.append(f"High CPU usage ({data['cpu_pct']:.1f}%)")

    if data.get("battery_temp") and data["battery_temp"] > 42:
        issues.append(f"CRITICAL thermal throttling risk — {data['battery_temp']}°C exceeds 42°C limit")
    elif data.get("battery_temp") and data["battery_temp"] > 39:
        issues.append(f"Device is getting hot — {data['battery_temp']}°C, approaching throttle zone")

    if data.get("mem_pss_mb", 0) > 3000:
        issues.append(f"High memory usage ({data['mem_pss_mb']:.0f} MB) — potential RAM bottleneck")

    fps = get_fps_stats(package_name)
    if status == "running" and fps.get("janky_frames", 0) > 5:
        issues.append(f"Frame drops detected — {fps['janky_frames']} janky frames")

    gpu = get_gpu_info()
    network = get_network_stats()

    result = {
        "status": status,
        "foreground_app": current_pkg,
        "raw_snapshot": data,
        "fps_data": fps,
        "gpu": gpu,
        "network": network,
        "diagnosed_issues": issues if issues else ["No major bottlenecks detected. Device is running smoothly."]
    }
    return json.dumps(result)

# ═══════════════════════════════════════════════════════════════
# MCP PROMPTS — Reusable prompt templates
# ═══════════════════════════════════════════════════════════════

@mcp.prompt()
def diagnose_game(package_name: str) -> str:
    """A pre-built prompt template for comprehensive game performance diagnosis."""
    return f"""Analyze the performance of the Android game '{package_name}' running on the connected device.

Please follow these steps:
1. First, check if the game is in the foreground using detect_foreground_app
2. Get a full device snapshot using get_full_snapshot
3. Get detailed FPS stats using get_fps  
4. Get thermal readings using get_thermal_stats
5. Get GPU stats using get_gpu_stats

Then provide a comprehensive diagnosis covering:
- Current FPS and frame stability
- CPU and GPU utilization
- Thermal state and throttling risk
- Memory pressure
- Network quality (if online game)
- Specific actionable recommendations to improve performance"""

# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    # Use stderr for logging so stdout remains clean for MCP JSON-RPC
    print("🚀 Starting Android Game Profiler MCP Server...", file=sys.stderr)
    print("📡 Transport: stdio", file=sys.stderr)
    print("🔧 Tools: 14 | Prompts: 1", file=sys.stderr)
    mcp.run(transport="stdio")
