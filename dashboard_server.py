import streamlit as st
import pandas as pd
import subprocess
import time
import re
from mcp.server.fastmcp import FastMCP

# 1. Setup MCP for the AI
mcp = FastMCP("Device-Monitor")

def get_adb_stat(cmd_args):
    # Running adb commands. cmd_args is a list of arguments.
    # Joining them appropriately since shell=True is used.
    # We use shell=True because we have pipes ('|') in our commands.
    cmd_str = " ".join(["adb"] + cmd_args)
    result = subprocess.run(cmd_str, capture_output=True, text=True, shell=True)
    return result.stdout.strip()

@mcp.tool()
def get_device_health():
    """Returns a dictionary of current thermal, cpu, and screen status."""
    # Thermal (macOS / Linux uses grep instead of findstr)
    temp_raw = get_adb_stat(["shell", "dumpsys", "battery", "|", "grep", "temperature"])
    # CPU (Top 1 line)
    cpu = get_adb_stat(["shell", "top", "-n", "1", "-m", "1", "|", "grep", "%"])
    # Screen Status
    screen = get_adb_stat(["shell", "dumpsys", "display", "|", "grep", "mScreenState"])

    temp_val = temp_raw.split()[-1] if temp_raw else "0"

    return {
        "temp": f"{int(temp_val)/10}°C" if temp_val.isdigit() else "N/A",
        "cpu": cpu if cpu else "Idle",
        "status": "Active" if "ON" in screen.upper() else "Frozen/Off"
    }

@mcp.tool()
def get_device_fps(package_name: str):
    """Returns the total frames rendered and janky frames for a specific package to calculate FPS/stutter."""
    raw = get_adb_stat(["shell", "dumpsys", "gfxinfo", package_name])
    janky = re.search(r"Janky frames: (\d+)", raw)
    total = re.search(r"Total frames rendered: (\d+)", raw)
    
    return {
        "package": package_name,
        "janky_frames": int(janky.group(1)) if janky else 0,
        "total_frames": int(total.group(1)) if total else 0,
    }

# 2. Setup Streamlit Dashboard UI
if __name__ == "__main__":
    st.set_page_config(layout="wide", page_title="DUT Performance Monitor")
    st.title("📱 Android DUT Live Dashboard")

    # Input for FPS tracking target package
    target_pkg = st.sidebar.text_input("FPS Package Name", "com.supercell.clashofclans")

    placeholder = st.empty()

    while True:
        stats = get_device_health()
        fps_stats = get_device_fps(target_pkg) if target_pkg else {"janky_frames": 0, "total_frames": 0}

        with placeholder.container():
            col1, col2, col3, col4 = st.columns(4)

            with col1:
                st.metric("Thermal Temp", stats['temp'])
            with col2:
                st.metric("Screen State", stats['status'])
            with col3:
                # Truncate CPU text for better metric display or use info block
                st.info(f"CPU Load: {stats['cpu'].strip()[:50]}")
            with col4:
                st.metric("FPS / Frames", fps_stats['total_frames'], f"-{fps_stats['janky_frames']} janky", delta_color="inverse")

            # Simple "Frozen" detection logic
            if "OFF" in stats['status'].upper():
                st.error("⚠️ ALERT: Device Screen is Inactive!")

        time.sleep(2) # Refresh every 2 seconds
