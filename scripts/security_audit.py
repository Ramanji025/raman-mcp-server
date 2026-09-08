"""Phase 6: security audit gate — runs bandit (SAST) and fails the build on
any HIGH-severity finding. LOW/MEDIUM findings are printed for visibility
but don't block, since several are reviewed/accepted patterns (local-dev
dashboard binding to 0.0.0.0, defensive try/except/pass) documented inline
with `# nosec` where a specific line was reviewed and judged safe.

    python scripts/security_audit.py

Exit code 0 = no HIGH findings. Exit code 1 = at least one HIGH finding
(build should fail). Requires the `security` extra: pip install -e ".[security]"
"""
from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed argv, invokes our own installed bandit only
import sys
from pathlib import Path

_REPORT_PATH = Path("data/security/bandit_report.json")


def main() -> int:
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "bandit", "-r", "src/mcp_kb",
           "-f", "json", "-o", str(_REPORT_PATH), "-q"]
    # bandit exits non-zero when it finds ANY issue (by design) — we handle
    # severity filtering ourselves below, so ignore its raw return code here.
    subprocess.run(cmd, check=False)  # nosec B603

    report = json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
    results = report.get("results", [])
    high = [r for r in results if r["issue_severity"] == "HIGH"]
    medium = [r for r in results if r["issue_severity"] == "MEDIUM"]
    low = [r for r in results if r["issue_severity"] == "LOW"]

    print(f"Bandit SAST scan: {len(results)} finding(s) "
          f"({len(high)} HIGH, {len(medium)} MEDIUM, {len(low)} LOW)")
    for finding in high:
        print(f"  HIGH   {finding['test_id']} {finding['filename']}:{finding['line_number']} "
              f"— {finding['issue_text']}", file=sys.stderr)
    for finding in medium + low:
        print(f"  {finding['issue_severity']:<6} {finding['test_id']} "
              f"{finding['filename']}:{finding['line_number']} — {finding['issue_text']}")

    if high:
        print(f"FAIL: {len(high)} HIGH-severity finding(s) must be fixed or "
              f"explicitly reviewed with a justified `# nosec` comment.", file=sys.stderr)
        return 1
    print("OK: no HIGH-severity findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
