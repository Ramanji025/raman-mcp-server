"""Backfill stable edge identities and queryable flow metadata in Neo4j.

Required once when graphs were migrated before Neo4jGraphStore began assigning
``edge_id`` and promoted relationship properties. Safe to rerun.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_kb.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=settings.neo4j_auth,
    )
    try:
        with driver.session(database=settings.neo4j_database) as session:
            rows = list(session.run(
                "MATCH (source:KBNode)-[relationship]->(target:KBNode) "
                "WHERE relationship.edge_id IS NULL "
                "RETURN elementId(relationship) AS relationship_id, source.id AS source_id, "
                "target.id AS target_id, type(relationship) AS relationship_type, "
                "relationship.attributes_json AS attributes_json"
            ))
            updates = []
            for row in rows:
                attrs_json = row["attributes_json"] or "{}"
                attrs = json.loads(attrs_json)
                edge_id = hashlib.sha256(
                    "\x00".join((row["source_id"], row["target_id"], row["relationship_type"], attrs_json)).encode("utf-8")
                ).hexdigest()
                updates.append({
                    "relationship_id": row["relationship_id"],
                    "edge_id": edge_id,
                    "seq": attrs.get("seq"),
                    "via": attrs.get("via"),
                    "role": attrs.get("role"),
                    "step": attrs.get("step"),
                })
            if updates:
                session.run(
                    "UNWIND $updates AS update "
                    "MATCH ()-[relationship]->() WHERE elementId(relationship) = update.relationship_id "
                    "SET relationship.edge_id = update.edge_id, relationship.seq = update.seq, "
                    "relationship.via = update.via, relationship.role = update.role, "
                    "relationship.step = update.step",
                    updates=updates,
                ).consume()
            total = session.run(
                "MATCH (:KBNode)-[relationship]->(:KBNode) RETURN count(relationship) AS count"
            ).single()["count"]
            identified = session.run(
                "MATCH (:KBNode)-[relationship {edge_id: null}]->(:KBNode) RETURN count(relationship) AS count"
            ).single()["count"]
    finally:
        driver.close()

    print(json.dumps({
        "relationships_backfilled": len(updates),
        "total_relationships": total,
        "relationships_without_edge_id": identified,
        "validation_passed": identified == 0,
    }, indent=2))
    if identified:
        raise SystemExit("Neo4j edge metadata backfill validation failed")


if __name__ == "__main__":
    main()
