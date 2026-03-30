import subprocess
import time
import re

# ─── ADB Helpers ──────────────────────────────────────────────

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
    try:
        raw = subprocess.run(
            "adb shell dumpsys window", shell=True,
            capture_output=True, text=True, timeout=8
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
    # Standard lightweight ADB polling (CPU/RAM/Temp)
    row = {"timestamp": ts}
    row.update(get_battery())
    row.update(get_memory(pkg))
    row["total_ram_mb"] = get_total_ram_mb()
    row.update(get_cpu(pkg))
    row.update(get_thermals())
    row.update(get_network_stats())
    return row

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
    connectivity = adb('"dumpsys connectivity | grep NetworkAgentInfo"')
    
    net_type = "Unknown"
    if "WIFI" in connectivity.upper():
        net_type = "WiFi"
    elif "MOBILE" in connectivity.upper() or "CELLULAR" in connectivity.upper():
        net_type = "Cellular"
        
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
    time.sleep(1.5)
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
            time.sleep(1.5)
            trace_raw = adb("atrace --async_dump -c gfx")
            adb("atrace --async_stop")
            
            elapsed_atrace = time.time() - t1
            if elapsed_atrace <= 0: elapsed_atrace = 1.5
            
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
        "estimated_fps_hz": estimated_fps,
        "elapsed_seconds": round(elapsed_atrace if total_frames_from_gfx == 0 else elapsed, 2)
    }
