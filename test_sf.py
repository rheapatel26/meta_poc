import subprocess, time

out = []
def log(s):
    out.append(str(s))
    print(s)

log("=== Approach 2: atrace event counting (2s) ===")
try:
    # Start atrace async
    subprocess.run('adb shell "atrace --async_start -c gfx view"', shell=True, capture_output=True, timeout=5)
    time.sleep(2.0)
    # Dump and stop
    r = subprocess.run('adb shell "atrace --async_dump -c gfx"', shell=True, capture_output=True, text=True, timeout=10)
    subprocess.run('adb shell "atrace --async_stop"', shell=True, capture_output=True, timeout=5)
    
    dump_out = r.stdout
    swap_count = dump_out.count("eglSwapBuffers")
    queue_count = dump_out.count("queueBuffer")
    do_frame_count = dump_out.count("doFrame")
    
    log(f"eglSwapBuffers events: {swap_count}")
    log(f"queueBuffer events: {queue_count}")
    log(f"doFrame events: {do_frame_count}")
    
    # max of these as they correlate with frame dispatches from engine
    max_events = max(swap_count, do_frame_count, queue_count)
    log(f"Estimated FPS from atrace: {max_events / 2.0:.1f}")
except Exception as e:
    log(f"atrace error: {e}")

with open("test_output2.txt", "w") as f:
    f.write("\n".join(out))
log("\nDone!")
