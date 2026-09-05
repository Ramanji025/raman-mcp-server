"""Online learner: aliases, boosts, implicit reformulation."""
from __future__ import annotations

from mcp_kb.config import Settings
from mcp_kb.learning.online import OnlineLearner, apply_retrieval_boosts, jaccard
from mcp_kb.models import Chunk, ContentType, RetrievedChunk
from mcp_kb.nlp.query_understand import understand


def _learner(tmp_path) -> OnlineLearner:
    return OnlineLearner(Settings(), root=tmp_path / "learning")


def test_aliases_rewrite_query(tmp_path):
    learner = _learner(tmp_path)
    learner._state.aliases["ordersvc"] = "rx-order-service"
    rewritten = learner.apply_aliases("what does ordersvc expose?")
    assert "rx-order-service" in rewritten


def test_understand_uses_aliases():
    u = understand(
        "what does ordersvc expose?",
        services=["rx-order-service"],
        aliases={"ordersvc": "rx-order-service"},
    )
    assert "rx-order-service" in u.expanded


def test_high_confidence_ask_boosts_chunks(tmp_path):
    learner = _learner(tmp_path)
    ev = learner.record_ask(
        question="How does eligibility work in rx-order-service?",
        expanded="eligibility rx-order-service",
        intent="general_search", handler="search_code",
        confidence=0.8,
        chunk_ids=["c-elig"],
        paths=["OrderService.java"],
        entities=["rx-order-service"],
    )
    assert ev.implicit == "positive"
    assert learner.retrieval_boosts()["c-elig"] > 0
    assert learner.retrieval_boosts()["OrderService.java"] > 0


def test_reformulation_after_weak_answer_is_negative(tmp_path):
    learner = _learner(tmp_path)
    learner.record_ask(
        question="customer eligibility checkout flow",
        expanded="customer eligibility checkout flow",
        intent="general_search", handler="search_code",
        confidence=0.2,
        chunk_ids=["bad-chunk"],
        paths=["noise.md"],
        entities=[],
    )
    learner.record_ask(
        question="customer eligibility checkout validation",
        expanded="customer eligibility checkout validation",
        intent="general_search", handler="search_code",
        confidence=0.7,
        chunk_ids=["good-chunk"],
        paths=["Validate.java"],
        entities=["rx-order-service"],
    )
    boosts = learner.retrieval_boosts()
    assert boosts.get("bad-chunk", 0) < 0
    assert boosts.get("good-chunk", 0) > 0


def test_explicit_rating_updates_state(tmp_path):
    learner = _learner(tmp_path)
    ev = learner.record_ask(
        question="kafka listener in payments",
        expanded="kafka listener payments",
        intent="general_search", handler="search_code",
        confidence=0.5,
        chunk_ids=["k1"], paths=["Pay.java"], entities=["PaymentService"],
    )
    rated = learner.rate(ev.id, 5, "correct file")
    assert rated is not None
    assert rated.rating == 5
    assert learner.state().n_ratings == 1
    assert learner.retrieval_boosts()["k1"] > 0


def test_apply_retrieval_boosts_reorders():
    a = RetrievedChunk(
        chunk=Chunk(id="a", repo="r", rel_path="a.java", content_type=ContentType.JAVA,
                    collection="code", text="a"),
        score=0.4, source="vector",
    )
    b = RetrievedChunk(
        chunk=Chunk(id="b", repo="r", rel_path="b.java", content_type=ContentType.JAVA,
                    collection="code", text="b"),
        score=0.5, source="vector",
    )
    out = apply_retrieval_boosts([a, b], {"b": 1.0})
    assert out[0].chunk.id == "b"


def test_jaccard_overlap():
    assert jaccard(["a", "b"], ["b", "c"]) == 1 / 3
