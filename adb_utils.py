import subprocess
import time
import re

# ─── ADB Helpers ──────────────────────────────────────────────

def adb(cmd):
    try:
        result = subprocess.run(
            f"adb shell {cmd}", shell=True,
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace'
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
    pss = re.search(r"TOTAL(?: PSS)?[:]?\s+(\d+)", raw)
    rss = re.search(r"TOTAL(?: RSS)?[:]?\s+(\d+)", raw)
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
    try:
        raw = subprocess.run(
            "adb shell dumpsys window", shell=True,
            capture_output=True, text=True, timeout=8,
            encoding='utf-8', errors='replace'
        ).stdout
        for line in raw.splitlines():
            if "mCurrentFocus" in line:
                m = re.search(r"u0\s+([\w.]+)/", line)
                if m: return m.group(1)
                m2 = re.search(r"\s([\w.]+)/[\w.]+\}", line)
                if m2: return m2.group(1)
            if "mFocusedApp" in line:
                m = re.search(r"u0\s+([\w.]+)/", line)
                if m: return m.group(1)
    except Exception:
        pass
    
    raw2 = adb('"dumpsys activity | grep mResumedActivity"')
    match = re.search(r"u0 ([\w.]+)/", raw2)
    return match.group(1) if match else None

def get_total_ram_mb():
    try:
        raw = adb('"cat /proc/meminfo | grep MemTotal"')
        match = re.search(r"(\d+)", raw)
        return int(match.group(1)) / 1024 if match else 0
    except:
        return 0

def collect_snapshot(pkg, ts):
    """Latency optimization: use the batched version for the monitoring loop."""
    data = batch_adb_snapshot(pkg)
    data["timestamp"] = ts
    return data

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

# Global state for RX/TX delta calculations
_prev_net_bytes = {"rx": None, "tx": None, "ts": None}

def get_network_stats():
    """Get live network connectivity, RSSI, and real TX/RX throughput."""
    global _prev_net_bytes

    # ── 1. Connectivity type ──────────────────────────────────────
    connectivity = adb('"dumpsys connectivity | grep NetworkAgentInfo"') or ""
    net_type = "Unknown"
    if "WIFI" in connectivity.upper():
        net_type = "WiFi"
    elif "MOBILE" in connectivity.upper() or "CELLULAR" in connectivity.upper():
        net_type = "Cellular"

    # ── 2. WiFi RSSI (try multiple ADB command variants) ─────────
    rssi_dbm = None
    link_speed_mbps = None

    # Try modern Android format first (Android 12+)
    wifi_raw = adb('"cmd wifi status"') or ""
    rssi_match = re.search(r"RSSI:\s*(-?\d+)", wifi_raw)
    if not rssi_match:
        # Fallback: older dumpsys wifi
        wifi_raw = adb('"dumpsys wifi"') or ""
        # mWifiInfo or WifiInfo block
        rssi_match = re.search(r"RSSI:\s*(-?\d+)", wifi_raw)
    if rssi_match:
        rssi_dbm = int(rssi_match.group(1))
    link_speed_match = re.search(r"Link speed:\s*(\d+)", wifi_raw)
    if link_speed_match:
        link_speed_mbps = int(link_speed_match.group(1))

    # ── 3. TX / RX bytes from /proc/net/dev (works on all types) ─
    net_dev = adb('"cat /proc/net/dev"') or ""
    total_rx, total_tx = 0, 0
    for line in net_dev.splitlines():
        # Skip loopback
        if 'lo:' in line or 'lo ' in line:
            continue
        parts = line.split()
        if len(parts) >= 10 and ':' in parts[0]:
            try:
                total_rx += int(parts[1])   # bytes received
                total_tx += int(parts[9])   # bytes transmitted
            except (ValueError, IndexError):
                pass

    now = time.time()
    rx_rate_kbps = None
    tx_rate_kbps = None
    if _prev_net_bytes["rx"] is not None and _prev_net_bytes["ts"] is not None:
        dt = now - _prev_net_bytes["ts"]
        if dt > 0:
            rx_rate_kbps = max(0, (total_rx - _prev_net_bytes["rx"]) / dt / 1024)
            tx_rate_kbps = max(0, (total_tx - _prev_net_bytes["tx"]) / dt / 1024)

    _prev_net_bytes["rx"] = total_rx
    _prev_net_bytes["tx"] = total_tx
    _prev_net_bytes["ts"] = now

    return {
        "network_type": net_type,
        "wifi_rssi_dbm": rssi_dbm,
        "wifi_link_speed_mbps": link_speed_mbps,
        "net_rx_kbps": round(rx_rate_kbps, 1) if rx_rate_kbps is not None else None,
        "net_tx_kbps": round(tx_rate_kbps, 1) if tx_rate_kbps is not None else None,
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
    seen = set()
    unique_apps = []
    for a in apps:
        if a not in seen:
            seen.add(a)
            unique_apps.append(a)
    return {"recent_apps": unique_apps[:10]}

def get_fps_stats(package_name):
    """Retrieve frame rendering stats (jank and total frames) using dumpsys gfxinfo."""
    package_name = package_name.strip()
    adb(f"dumpsys gfxinfo {package_name} reset")
    
    t0 = time.time()
    time.sleep(5)  # Increased sampling window to 5s for better moving average
    elapsed = time.time() - t0
    
    raw = adb(f"dumpsys gfxinfo {package_name}")
    janky = re.search(r"Janky frames: (\d+)", raw)
    total = re.search(r"Total frames rendered: (\d+)", raw)
    
    janky_frames = int(janky.group(1)) if janky else 0
    total_frames = int(total.group(1)) if total else 0
    total_frames_from_gfx = total_frames
    estimated_fps = 0.0
    elapsed_atrace = elapsed
    
    if total_frames > 0:
        estimated_fps = round(total_frames / elapsed, 1)
    
    # Method 2: Fallback to atrace if gfxinfo returns 0 (Unreal/Unity games)
    if total_frames == 0:
        try:
            adb("atrace --async_start -b 16384 -c gfx view")
            t1 = time.time()
            time.sleep(5)  # Increased sampling window to 5s
            trace_raw = adb("atrace --async_dump -c gfx")
            adb("atrace --async_stop")
            
            elapsed_atrace = time.time() - t1
            if elapsed_atrace <= 0: elapsed_atrace = 5.0
            
            swap_count = trace_raw.count("eglSwapBuffers")
            queue_count = trace_raw.count("queueBuffer")
            do_frame_count = trace_raw.count("doFrame")
            
            if swap_count > 5:
                total_frames = swap_count
            elif do_frame_count > 5:
                total_frames = do_frame_count
            elif queue_count > 5:
                total_frames = int(queue_count / 2)
            
            if total_frames > 0:
                estimated_fps = round(total_frames / elapsed_atrace, 1)
        except Exception:
            pass

    return {
        "janky_frames": janky_frames,
        "total_frames_measured": total_frames, 
        "moving_average_fps": estimated_fps,
        "sampling_window_seconds": round(elapsed_atrace if total_frames_from_gfx == 0 else elapsed, 2)
    }

def batch_adb_snapshot(pkg):
    """Latency multi-kill: Run 8+ ADB commands in a single shell session."""
    ts = time.time()
    cmd = (
        "echo '==ADB_SNAP_BATT=='; dumpsys battery; "
        f"echo '==ADB_SNAP_MEM=='; dumpsys meminfo {pkg}; "
        "echo '==ADB_SNAP_CPU=='; dumpsys cpuinfo; "
        "echo '==ADB_SNAP_THERM=='; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null; "
        "echo '==ADB_SNAP_NET=='; cat /proc/net/dev; "
        "echo '==ADB_SNAP_GPU=='; cat /sys/class/kgsl/kgsl-3d0/gpuclk 2>/dev/null; "
        "cat /sys/class/kgsl/kgsl-3d0/gpubusy 2>/dev/null; "
        "cat /sys/class/kgsl/kgsl-3d0/devfreq/governor 2>/dev/null; "
        "echo '==ADB_SNAP_DISP=='; wm size; wm density; dumpsys display | grep mRefreshRate; "
        "echo '==ADB_SNAP_FG=='; dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'; "
        "echo '==ADB_SNAP_MEMTOTAL=='; cat /proc/meminfo | grep MemTotal; "
        f"echo '==ADB_SNAP_GFX=='; dumpsys gfxinfo {pkg} | grep -E 'Total frames rendered|Janky frames'; "
        "echo '==ADB_SNAP_WIFI=='; dumpsys wifi | grep -E 'RSSI|mWifiInfo'"
    )
    full_raw = adb(f'"{cmd}"')
    sections = {}
    current_sec = None
    for line in full_raw.splitlines():
        line_clean = line.strip()
        if line_clean.startswith("==ADB_SNAP_") and line_clean.endswith("=="):
            current_sec = line_clean.strip("=").replace("ADB_SNAP_", "")
            sections[current_sec] = ""
        elif current_sec:
            sections[current_sec] += line + "\n"

    b_raw = sections.get('BATT', '')
    level = re.search(r"level: (\d+)", b_raw)
    temp  = re.search(r"temperature: (\d+)", b_raw)
    volt  = re.search(r"voltage: (\d+)", b_raw)

    m_raw = sections.get('MEM', '')
    pss = re.search(r"TOTAL(?: PSS)?[:]?\s+(\d+)", m_raw)
    rss = re.search(r"TOTAL(?: RSS)?[:]?\s+(\d+)", m_raw)

    c_raw = sections.get('CPU', '')
    cpu_match = re.search(rf"([\d.]+)% .+{re.escape(pkg)}", c_raw)

    t_raw = sections.get('THERM', '')
    t_vals = [int(t)/1000 for t in t_raw.split() if t.isdigit() and int(t) < 200000]

    net_raw = sections.get('NET', '')
    total_rx, total_tx = 0, 0
    for line in net_raw.splitlines():
        if 'lo:' in line or 'lo ' in line: continue
        parts = line.split()
        if len(parts) >= 10 and ':' in parts[0]:
            try:
                total_rx += int(parts[1])
                total_tx += int(parts[9])
            except: pass
    
    global _prev_net_bytes
    rx_rate, tx_rate = 0.0, 0.0
    if _prev_net_bytes["rx"] is not None:
        dt = ts - _prev_net_bytes["ts"]
        if dt > 0:
            rx_rate = max(0, (total_rx - _prev_net_bytes["rx"]) / dt / 1024)
            tx_rate = max(0, (total_tx - _prev_net_bytes["tx"]) / dt / 1024)
    _prev_net_bytes.update({"rx": total_rx, "tx": total_tx, "ts": ts})

    g_raw = sections.get('GPU', '').splitlines()
    gpu_freq = g_raw[0] if len(g_raw) > 0 else "N/A"
    gpu_busy = g_raw[1] if len(g_raw) > 1 else "N/A"
    gpu_gov = g_raw[2] if len(g_raw) > 2 else "N/A"

    d_raw = sections.get('DISP', '')
    size_m = re.search(r"(\d+x\d+)", d_raw)
    dens_m = re.search(r"(\d+)", d_raw)
    refr_m = re.search(r"([\d.]+)", d_raw)

    fg_raw = sections.get('FG', '')
    fg_pkg = None
    m = re.search(r"u0\s+([\w.]+)/", fg_raw)
    if m: fg_pkg = m.group(1)
    if not fg_pkg:
        m2 = re.search(r"\s([\w.]+)/[\w.]+\}", fg_raw)
        if m2: fg_pkg = m2.group(1)

    mr_raw = sections.get('MEMTOTAL', '')
    total_ram_m = re.search(r"(\d+)", mr_raw)
    total_ram = int(total_ram_m.group(1)) / 1024 if total_ram_m else 0

    # --- GFX frame counters (for FPS delta estimation) ---
    gfx_raw = sections.get('GFX', '')
    gfx_total_m = re.search(r"Total frames rendered: (\d+)", gfx_raw)
    gfx_janky_m = re.search(r"Janky frames: (\d+)", gfx_raw)

    # --- WiFi RSSI ---
    wifi_raw = sections.get('WIFI', '')
    rssi_m = re.search(r"RSSI:\s*(-?\d+)", wifi_raw)

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "battery_pct":  int(level.group(1)) if level else None,
        "battery_temp": int(temp.group(1)) / 10 if temp else None,
        "battery_volt": int(volt.group(1)) / 1000 if volt else None,
        "mem_pss_mb": int(pss.group(1)) / 1024 if pss else None,
        "mem_rss_mb": int(rss.group(1)) / 1024 if rss else None,
        "cpu_pct": float(cpu_match.group(1)) if cpu_match else 0.0,
        "max_thermal_c": max(t_vals) if t_vals else None,
        "network_type": "WiFi" if "WIFI" in d_raw.upper() else "Cellular",
        "net_rx_kbps": round(rx_rate, 1),
        "net_tx_kbps": round(tx_rate, 1),
        "gpu_clock_hz": gpu_freq.strip(),
        "gpu_busy": gpu_busy.strip(),
        "gpu_governor": gpu_gov.strip(),
        "resolution": size_m.group(1) if size_m else "N/A",
        "refresh_rate_hz": float(refr_m.group(1)) if refr_m else None,
        "foreground_app": fg_pkg,
        "game_is_foreground": fg_pkg == pkg,
        "total_ram_mb": total_ram,
        "gfx_total_frames": int(gfx_total_m.group(1)) if gfx_total_m else 0,
        "gfx_janky_frames": int(gfx_janky_m.group(1)) if gfx_janky_m else 0,
        "wifi_rssi_dbm": int(rssi_m.group(1)) if rssi_m else None,
    }
