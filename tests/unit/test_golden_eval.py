from mcp_kb.eval.golden import GOLDEN_QUESTIONS, score_answer, score_faithfulness, score_pack_markdown


def test_score_answer_passes_on_substring():
    g = {"id": "t", "must_contain_any": ["eligib", "zzz"]}
    r = score_answer("Eligibility is checked in validateCustomer", g)
    assert r["passed"] is True
    assert "eligib" in r["hits"]


def test_score_answer_fails_when_nothing_matches():
    r = score_answer("unrelated text", {"id": "t", "must_contain_any": ["eligib"]})
    assert r["passed"] is False
    assert r["missing"] == ["eligib"]


def test_score_pack_markdown_reports_rate():
    blob = "transactional rollback across services that depend on each other"
    summary = score_pack_markdown(blob, GOLDEN_QUESTIONS)
    assert summary["total"] == len(GOLDEN_QUESTIONS)
    assert 0 <= summary["pass_rate"] <= 1
    assert summary["passed"] >= 1


def test_score_faithfulness_flags_unsupported_entity():
    pack_md = "OrderService handles order placement."
    answer = "The flow touches OrderService and then PaymentService for billing."
    result = score_faithfulness(answer, pack_md)
    assert result["faithful"] is False
    assert "PaymentService" in result["unsupported_entities"]
    assert "OrderService" not in result["unsupported_entities"]


def test_score_faithfulness_passes_when_grounded():
    pack_md = "OrderService handles order placement and calls InventoryService."
    answer = "OrderService coordinates with InventoryService to reserve stock."
    result = score_faithfulness(answer, pack_md)
    assert result["faithful"] is True
