import glob
from perfetto.trace_processor import TraceProcessor
import pandas as pd

with open('out.txt', 'w') as f:
    trace_files = glob.glob('trace_jp.konami.pesam_*.perfetto-trace')
    if not trace_files:
        f.write('No pesam traces found.\n')
    else:
        trace_path = sorted(trace_files)[-1]
        tp = TraceProcessor(trace=trace_path)
        
        f.write('--- frametimeline count by layer ---\n')
        df1 = tp.query('''
            SELECT layer_name, COUNT(*) as frames, SUM(dur)/1000000 as total_dur_ms 
            FROM actual_frame_timeline_slice 
            WHERE present_type = 'PRESENTED'
            GROUP BY layer_name ORDER BY frames DESC LIMIT 10
        ''').as_pandas_dataframe()
        f.write(df1.to_string() + '\n\n')
        
        f.write('--- pesam top slices ---\n')
        df2 = tp.query('''
            SELECT s.name, COUNT(*) as c
            FROM slice s
            JOIN thread_track tt ON s.track_id = tt.id
            JOIN thread t ON tt.utid = t.utid
            JOIN process p ON t.upid = p.upid
            WHERE p.name LIKE '%pesam%' AND s.name NOT LIKE '%lock%' AND s.name NOT LIKE '%monitor%'
            GROUP BY s.name ORDER BY c DESC LIMIT 20
        ''').as_pandas_dataframe()
        f.write(df2.to_string() + '\n')
