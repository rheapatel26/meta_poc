from perfetto.trace_processor import TraceProcessor

import sys
try:
    tp = TraceProcessor(trace='trace.perfetto-trace')

    qr_fps = tp.query("""
        SELECT COUNT(id) AS frames,
               (MAX(ts) - MIN(ts)) / 1e9 as duration_sec
        FROM slice
        WHERE name LIKE '%Choreographer#doFrame%' AND process_name LIKE '%konami%'
    """)

    for row in qr_fps:
        frames = row.frames
        dur = row.duration_sec
        fps = frames / dur if dur and dur > 0 else 0
        print(f"Frames: {frames}, duration: {dur:.2f}s, FPS: {fps:.1f}")
        
except Exception as e:
    print(f"Error: {e}")
