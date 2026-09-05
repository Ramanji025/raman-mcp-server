"""Neo4j + Qdrant backed persistence for the online learner (Phase 1).

Replaces the flat ``state.json``/``events.jsonl`` files with:
  * Neo4j: the feedback graph -- ``(:LQuery)-[:MATCHED]->(:LChunk)``,
    ``(:LQuery)-[:RATED {rating}]->(:LQuery)``, ``(:LAlias)-[:MAPS_TO]->(:LEntity)``.
    This is what later feeds the LightGBM training pipeline.
  * Qdrant: the numeric boost is written onto the *existing* chunk points as
    payload fields (``learn_boost``, ``learn_pos``, ``learn_neg``) so it is
    applied natively at retrieval time and travels with the vector.

Falls back to the file-based ``OnlineLearner`` behaviour (in-memory state) for
everything else -- scoring math is inherited, only persistence is swapped.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..config import Settings
from ..logging import get_logger
from .online import InteractionEvent, OnlineLearner

log = get_logger(__name__)


class GraphOnlineLearner(OnlineLearner):
    """``OnlineLearner`` whose state lives in Neo4j (graph) + Qdrant (boosts)."""

    def __init__(self, settings: Settings, *, root: Any = None) -> None:
        self._settings = settings
        self._driver = None
        self._qdrant = None
        super().__init__(settings, root=root)  # still keeps a local events.jsonl mirror

    # ------------------------------------------------------------------ #
    def _neo4j(self):
        if self._driver is None:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self._settings.neo4j_uri, auth=self._settings.neo4j_auth,
            )
        return self._driver

    def _qdrant_client(self):
        if self._qdrant is None:
            from qdrant_client import QdrantClient

            self._qdrant = QdrantClient(
                url=self._settings.qdrant_url,
                api_key=self._settings.qdrant_api_key or None,
                check_compatibility=False,
            )
        return self._qdrant

    # ------------------------------------------------------------------ #
    def _append_event(self, event: InteractionEvent) -> None:
        # Keep the cheap local mirror (training reads it) ...
        super()._append_event(event)
        # ... and write the durable graph record.
        try:
            with self._neo4j().session(database=self._settings.neo4j_database) as session:
                session.execute_write(self._write_event_tx, event)
        except Exception as exc:
            log.warning("learning_graph_write_failed", error=str(exc))

    @staticmethod
    def _write_event_tx(tx, event: InteractionEvent) -> None:
        tx.run(
            """
            MERGE (q:LQuery {id: $id})
            SET q.question = $question, q.ts = $ts, q.intent = $intent,
                q.handler = $handler, q.confidence = $confidence,
                q.rating = $rating, q.implicit = $implicit
            WITH q
            UNWIND $chunk_ids AS cid
              MERGE (c:LChunk {id: cid})
              MERGE (q)-[:MATCHED]->(c)
            WITH q
            UNWIND $entities AS ent
              MERGE (e:LEntity {name: ent})
              MERGE (q)-[:ABOUT]->(e)
            """,
            id=event.id, question=event.question, ts=event.ts, intent=event.intent,
            handler=event.handler, confidence=event.confidence, rating=event.rating,
            implicit=event.implicit or "", chunk_ids=event.chunk_ids, entities=event.entities,
        )

    def _learn_aliases(self, event: InteractionEvent) -> None:
        super()._learn_aliases(event)
        try:
            with self._neo4j().session(database=self._settings.neo4j_database) as session:
                for term, entity in self._state.aliases.items():
                    session.run(
                        "MERGE (a:LAlias {term: $term}) MERGE (e:LEntity {name: $entity}) "
                        "MERGE (a)-[:MAPS_TO]->(e)",
                        term=term, entity=entity,
                    )
        except Exception as exc:
            log.warning("learning_alias_graph_write_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    def _save_state(self) -> None:
        # Keep the local JSON as a cache/fallback ...
        super()._save_state()
        # ... then push the current boosts onto the Qdrant chunk payloads.
        self._sync_qdrant_boosts()

    def _sync_qdrant_boosts(self) -> None:
        boosts = self._state.chunk_boosts
        if not boosts:
            return
        try:
            client = self._qdrant_client()
            for collection in self._settings.static.get(
                "vector", {}
            ).get("collections", ["semantic", "code", "docs", "architecture", "defects", "incidents"]):
                for chunk_id, boost in boosts.items():
                    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))
                    try:
                        client.set_payload(
                            collection_name=collection,
                            payload={"learn_boost": round(float(boost), 4)},
                            points=[point_id],
                        )
                    except Exception:
                        continue  # point may not exist in this collection
        except Exception as exc:
            log.warning("learning_qdrant_sync_failed", error=str(exc))
