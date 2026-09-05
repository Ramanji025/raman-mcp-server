"""Reproduce: close stdin early (as VS Code does on timeout) and watch for a
flood of '\\n' JSON parse errors on stderr."""
import subprocess
import threading
import time

EXE = r".\.venv\Scripts\mcp-kb-server.exe"
proc = subprocess.Popen(
    [EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)

errs = []


def drain(stream):
    for line in stream:
        errs.append(line.decode("utf-8", "replace").rstrip())


threading.Thread(target=drain, args=(proc.stderr,), daemon=True).start()
threading.Thread(target=drain, args=(proc.stdout,), daemon=True).start()

# Simulate VS Code closing the pipe before the (slow) server finishes starting.
time.sleep(2.0)
proc.stdin.close()

# Watch for ~35s to let the import finish and see if it busy-loops on EOF.
time.sleep(35.0)
invalid = sum(1 for e in errs if "Invalid JSON" in e)
print("returncode:", proc.poll())
print("Invalid-JSON error count after stdin EOF:", invalid)
print("total stderr lines:", len(errs))
try:
    proc.terminate()
except Exception:
    pass
