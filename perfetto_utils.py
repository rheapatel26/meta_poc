import subprocess
import time
import pandas as pd
from perfetto.trace_processor import TraceProcessor

def run_perfetto_trace(pkg, duration_sec):
    config = f"""
buffers {{
    size_kb: 32768
    fill_policy: RING_BUFFER
}}
write_into_file: true
file_write_period_ms: 2500
data_sources {{ config {{ name: "android.surfaceflinger.frametimeline" }} }}
data_sources {{ config {{ name: "android.surfaceflinger" }} }}
data_sources {{ config {{ name: "linux.ftrace"
        ftrace_config {{
            ftrace_events: "ftrace/print"
            ftrace_events: "power/cpu_frequency"
            ftrace_events: "power/cpu_idle"
            atrace_categories: "gfx"
            atrace_categories: "view"
            atrace_categories: "wm"
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
    
    time.sleep(2.0) # Wait for flush
    trace_path = f"trace_{pkg}_{int(time.time())}.perfetto-trace"
    subprocess.run(f"adb pull /data/misc/perfetto-traces/trace.perfetto-trace {trace_path}", shell=True)
    return trace_path

def parse_perfetto_trace(trace_path, pkg, target_fps=60):
    try:
        tp = TraceProcessor(trace=trace_path)
    except Exception as e:
        print(f"Failed to initialize trace processor: {e}")
        return pd.DataFrame(), f"init_error: {e}"

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
            short_pkg = pkg.split('.')[-1]
            layer_df = tp.query(f"""
                SELECT 
                  COALESCE(layer_name, 'None') as layer_name,
                  COUNT(*) as c,
                  SUM(CASE WHEN layer_name LIKE '%{pkg}%' OR layer_name LIKE '%{short_pkg}%' THEN 1000 ELSE 0 END) as priority_score
                FROM actual_frame_timeline_slice
                WHERE present_type LIKE '%Present%'
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
                  WHERE present_type LIKE '%Present%'
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
            query = f"""
            WITH app_slices AS (
              SELECT s.ts, s.dur
              FROM slice s
              JOIN thread_track tt ON s.track_id = tt.id
              JOIN thread t        ON tt.utid = t.utid
              JOIN process p       ON t.upid = p.upid
              WHERE (p.name LIKE '%{pkg}%' OR p.name LIKE '%{short_pkg}%')
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
            if df_results.empty:
                query2 = f"""
                WITH app_slices AS (
                  SELECT s.ts, s.dur
                  FROM slice s
                  JOIN thread_track tt ON s.track_id = tt.id
                  JOIN thread t        ON tt.utid = t.utid
                  JOIN process p       ON t.upid = p.upid
                  WHERE (p.name LIKE '%{pkg}%' OR p.name LIKE '%{short_pkg}%')
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
              WHERE (process.name LIKE '%{pkg}%' OR process.name LIKE '%{short_pkg}%')
                AND dur > 1000000
            ),
            min_ts AS (SELECT MIN(ts) as start_ts FROM gpu_slices),
            bucketed AS (
              SELECT
                CAST((ts-(SELECT start_ts FROM min_ts))/1e9 AS INT) AS time_s,
                CAST((ts-(SELECT start_ts FROM min_ts))/16666666 AS INT) AS vsync_window,
                ts/1e6  AS ts_ms,
                dur/1e6 AS dur_ms,
                0 AS is_jank
              FROM gpu_slices
            ),
            unique_frames AS (
              SELECT time_s, vsync_window, MIN(ts_ms) as ts_ms, MAX(dur_ms) as dur_ms, MAX(is_jank) as is_jank
              FROM bucketed
              GROUP BY time_s, vsync_window
            )
            SELECT time_s, ts_ms, dur_ms, is_jank,
              COUNT(*) OVER (PARTITION BY time_s) AS fps_at_sec
            FROM unique_frames ORDER BY ts_ms
            """
            df_results = tp.query(query).as_pandas_dataframe()
            
            if not df_results.empty:
                 df_results['fps_at_sec'] = df_results['fps_at_sec'].clip(upper=int(target_fps * 1.05))
            method_used = "gpu_slices_fallback"
        except Exception as e:
            print(f"GPU slice fallback failed: {e}")

    tp.close()
    return df_results, method_used
