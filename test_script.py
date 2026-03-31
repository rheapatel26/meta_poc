import glob
import os
from perfetto.trace_processor import TraceProcessor

traces = glob.glob('*.perfetto-trace')
if not traces:
    print("NO TRACES")
    exit(1)

recent = sorted(traces, key=os.path.getmtime)[-1]
print("Using trace:", recent)

tp = TraceProcessor(trace=recent)
df = tp.query('SELECT COUNT(*) as c FROM slice').as_pandas_dataframe()
print("Total slices:", df.iloc[0]['c'])

df2 = tp.query('SELECT name FROM trace_bounds').as_pandas_dataframe()
print(df2)
