"""Find which stdin byte pattern triggers the '\\n' Invalid-JSON error."""
import json
import subprocess
import threading
import time

EXE = r".\.venv\Scripts\mcp-kb-server.exe"

INIT = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "probe", "version": "1"}},
}).encode("utf-8")


def run_case(name, term, extra=b""):
    proc = subprocess.Popen(
        [EXE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    errs, outs = [], []
    threading.Thread(target=lambda: [errs.append(l) for l in proc.stderr],
                     daemon=True).start()
    threading.Thread(target=lambda: [outs.append(l) for l in proc.stdout],
                     daemon=True).start()
    # wait for slow import
    time.sleep(26)
    proc.stdin.write(INIT + term + extra)
    proc.stdin.flush()
    time.sleep(2)
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
    bad = sum(1 for l in errs if b"Invalid JSON" in l)
    got_init = any(b'"id":1' in l and b'"result"' in l for l in outs)
    print(f"[{name}] init_ok={got_init}  invalid_json_errors={bad}")


run_case("LF", b"\n")
run_case("CRLF", b"\r\n")
run_case("LF+blankLF", b"\n", b"\n")
run_case("CRLF+blankCRLF", b"\r\n", b"\r\n")
