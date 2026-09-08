"""Follow-on gap 1: performance-path tests for QdrantStore (mocked client,
no live Qdrant needed) — verifies upsert uses the batched/parallel
upload_points primitive with the configured knobs, existence checks are
cached (not re-issued per call), and delete_by_ids uses a point-id selector
(not a filter scan)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_kb.models import Chunk, ContentType
from mcp_kb.vector.qdrant_store import QdrantStore


def _make_store(settings) -> tuple[QdrantStore, MagicMock]:
    with patch("mcp_kb.vector.qdrant_store.QdrantClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        store = QdrantStore(settings, dim=8)
    return store, mock_client


def _chunk(chunk_id: str) -> Chunk:
    return Chunk(id=chunk_id, repo="order-service", rel_path="Foo.java",
                content_type=ContentType.JAVA, collection="code", text="hello world")


def test_upsert_uses_batched_parallel_upload_points(settings):
    settings.qdrant_upsert_batch_size = 128
    settings.qdrant_upsert_parallel = 4
    settings.qdrant_upsert_wait = False
    store, client = _make_store(settings)
    store._known_existing.add(settings.collection_name("code"))
    store._layouts[settings.collection_name("code")] = "unnamed"

    store.upsert([_chunk("c1")], [[0.1] * 8])

    client.upload_points.assert_called_once()
    _, kwargs = client.upload_points.call_args
    assert kwargs["batch_size"] == 128
    assert kwargs["parallel"] == 4
    assert kwargs["wait"] is False
    client.upsert.assert_not_called()  # must not fall back to the old blocking call


def test_upsert_wait_override_takes_precedence_over_setting(settings):
    settings.qdrant_upsert_wait = False
    store, client = _make_store(settings)
    name = settings.collection_name("code")
    store._known_existing.add(name)
    store._layouts[name] = "unnamed"

    store.upsert([_chunk("c1")], [[0.1] * 8], wait=True)

    assert client.upload_points.call_args.kwargs["wait"] is True


def test_search_skips_collection_exists_check_once_cached(settings):
    store, client = _make_store(settings)
    name = settings.collection_name("code")
    store._known_existing.add(name)
    store._layouts[name] = "unnamed"
    client.query_points.return_value = MagicMock(points=[])

    store.search("code", [0.1] * 8, top_k=5)

    client.collection_exists.assert_not_called()


def test_delete_by_ids_uses_point_id_selector_not_filter_scan(settings):
    store, client = _make_store(settings)
    for content_type in store._collections:
        store._known_existing.add(settings.collection_name(content_type))

    store.delete_by_ids(["c1", "c2"])

    assert client.delete.call_count == len(store._collections)
    for call in client.delete.call_args_list:
        selector = call.kwargs["points_selector"]
        assert hasattr(selector, "points")  # PointIdsList, not FilterSelector
        assert len(selector.points) == 2


def test_grpc_settings_passed_to_client(settings):
    settings.qdrant_prefer_grpc = True
    settings.qdrant_grpc_port = 7777
    with patch("mcp_kb.vector.qdrant_store.QdrantClient") as mock_cls:
        QdrantStore(settings, dim=8)
    _, kwargs = mock_cls.call_args
    assert kwargs["prefer_grpc"] is True
    assert kwargs["grpc_port"] == 7777
