from perfetto.trace_processor import TraceProcessor
import sys

try:
    tp = TraceProcessor(trace='trace_jp.konami.pesam_1774345006.perfetto-trace')
    
    # Check what process names are in the trace
    print("----- Processes containing pesam -----")
    qr = tp.query("SELECT name, upid FROM process WHERE name LIKE '%pesam%'")
    for row in qr:
        print(row.name, row.upid)

    print("\n----- SurfaceFlinger FrameTimeline Slices -----")
    qr = tp.query("SELECT COUNT(*) as c FROM actual_frame_timeline_slice")
    for row in qr:
        print("Count:", row.c)

    print("\n----- All Process Names -----")
    qr = tp.query("SELECT DISTINCT name FROM process LIMIT 50")
    for row in qr:
        print(row.name)

    print("\n----- All Slices with pesam -----")
    qr = tp.query("SELECT slice.name as sname, COUNT(*) as c FROM slice JOIN thread_track ON slice.track_id = thread_track.id JOIN thread USING(utid) JOIN process USING(upid) WHERE process.name LIKE '%pesam%' GROUP BY sname ORDER BY c DESC LIMIT 20")
    for row in qr:
        print(row.sname, row.c)

except Exception as e:
    print(f"Error: {e}")
