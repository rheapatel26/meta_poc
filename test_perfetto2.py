from perfetto.trace_processor import TraceProcessor
import pandas as pd

try:
    tp = TraceProcessor(trace='trace.perfetto-trace')
    
    query = """
    SELECT 
        slice.name,
        slice.ts / 1000000.0 as ts_ms,
        slice.dur / 1000000.0 as dur_ms,
        process.name as process_name
    FROM slice
    JOIN thread_track ON slice.track_id = thread_track.id
    JOIN thread USING(utid)
    JOIN process USING(upid)
    WHERE process.name LIKE '%pesam%' AND slice.name LIKE '%Choreographer#doFrame%'
    ORDER BY ts ASC
    """
    
    qr = tp.query(query)
    df = qr.as_pandas_dataframe()
    print("DataFrame rows:", len(df))
    if not df.empty:
        print(df.head())
        fps = len(df) / ((df['ts_ms'].max() - df['ts_ms'].min()) / 1000)
        print(f"Calculated FPS: {fps:.1f}")

except Exception as e:
    print(f"Error: {e}")
