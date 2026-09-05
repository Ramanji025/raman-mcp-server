"""CLI: score golden questions against the live `ask` tool when the KB is up.

    python -m mcp_kb.eval.cli

Tracks pass-rate + faithfulness history in data/eval/history.jsonl and exits
non-zero on any failure OR on a pass-rate regression vs. the last recorded run,
so this can be wired into a CI gate.

Offline unit tests use score_pack_markdown without this entry point.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .golden import GOLDEN_QUESTIONS, score_answer, score_faithfulness

_HISTORY_PATH = Path("data/eval/history.jsonl")


def _load_last_run() -> dict | None:
    if not _HISTORY_PATH.exists():
        return None
    last = None
    for line in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
    return last


def _append_run(record: dict) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    """CLI entrypoint: run the golden-question eval suite against a live KnowledgeService."""
    from ..config import get_settings
    from ..tools.knowledge_service import KnowledgeService

    svc = KnowledgeService(get_settings())
    results = []
    faithfulness = []
    for golden in GOLDEN_QUESTIONS:
        resp = svc.ask(golden["question"])
        answer = resp.markdown or resp.summary
        results.append(score_answer(answer, golden))
        pack_md = json.dumps((resp.data or {}).get("pack") or {})
        f = score_faithfulness(answer, pack_md)
        f["id"] = golden["id"]
        faithfulness.append(f)

    passed = sum(1 for r in results if r["passed"])
    unfaithful = [f for f in faithfulness if not f["faithful"]]
    pass_rate = round(passed / len(results), 3) if results else 0.0

    print(f"golden_eval {passed}/{len(results)} (pass_rate={pass_rate:.0%})")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  {status} {r['id']} hits={r['hits']} missing={r['missing']}")

    print(f"\nfaithfulness {len(faithfulness) - len(unfaithful)}/{len(faithfulness)}")
    for f in unfaithful:
        print(f"  UNSUPPORTED {f['id']} entities={f['unsupported_entities']}")

    last_run = _load_last_run()
    regressed = bool(last_run and pass_rate < last_run.get("pass_rate", 0.0))
    if regressed:
        print(
            f"\n⚠️  REGRESSION: pass_rate {pass_rate:.0%} < previous run "
            f"{last_run['pass_rate']:.0%} ({last_run.get('at')})"
        )

    _append_run({
        "at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "total": len(results),
        "pass_rate": pass_rate,
        "faithful": len(faithfulness) - len(unfaithful),
        "faithfulness_total": len(faithfulness),
    })

    ok = passed == len(results) and not unfaithful and not regressed
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

