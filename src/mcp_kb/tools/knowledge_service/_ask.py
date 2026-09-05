"""KnowledgeService mixin: AskMixin (ask domain).

Auto-generated split — see scripts/_split_knowledge_service.py.
"""
from __future__ import annotations

from ...logging import get_logger
from ...models import NodeType, ToolResponse
from ...retrieval.rag import citations_from

log = get_logger(__name__)


from ._helpers import _intent_collections, _intent_graph_hops


class AskMixin:
    """The `ask` router tool plus rating/learning-status endpoints."""
    def ask(self, question: str) -> ToolResponse:
        """Answer free-text questions via NLP → hybrid retrieve → evidence pack.

        Structured high-confidence routes (ticket diffs, named service profile)
        still dispatch to specialized tools. Everything else is extractive:
        the pack is the answer; a local LLM may only rephrase it.
        """
        from ...nlp.evidence_pack import build_evidence_pack
        from ...nlp.query_understand import llm_rewrite_query, understand
        from ...retrieval.rag import synthesize_from_pack
        from ...retrieval.web_search import search_web, should_use_web

        extra = self._ask_entity_names()
        aliases = self.learner.state().aliases if self.learner.enabled else {}
        extra_terms = self.learner.extra_expansion_terms(question) if self.learner.enabled else []
        understood = understand(
            question, services=self.graph.services(), extra_names=extra,
            aliases=aliases, extra_terms=extra_terms,
        )
        route = understood.route
        # P2.4: LLM rewrite for short/ambiguous queries before retrieval
        query = llm_rewrite_query(understood.expanded or question, self.llm)

        structured = {
            "get_defect_changes", "explain_service", "explain_architecture",
            "find_endpoint", "kafka_topology", "security_audit",
            "analyze_defect", "implement_feature", "trace_business_flow",
            "impact_analysis", "onboarding_assistant",
            "inspect_service", "inspect_application",
        }
        if route and route.handler in structured and route.confidence >= 0.8:
            primary = self._dispatch_ask(route, question)
            pack = build_evidence_pack(
                question, None,
                intent=route.intent, handler=route.handler,
                tool_markdown=primary.markdown or primary.summary,
                query_tokens=understood.tokens,
            )
            md = (primary.markdown or pack.as_markdown())
            header = (
                f"_Intent: `{route.intent}` → `{route.handler}` "
                f"(confidence {route.confidence:.0%}; nlp expanded query used "
                f"only for hybrid routes)_\n\n"
            )
            return self._ask_response(
                question=question, expanded=query, route=route, pack=pack,
                understood=understood, markdown=header + md,
                citations=primary.citations or [], extra_data={"primary": primary.data},
                summary=primary.summary, chunk_ids=[], paths=[],
                repos=[c.get("repo") for c in (primary.citations or []) if c.get("repo")],
                web_used=False,
            )

        retrieval = self.retriever.retrieve(
            query,
            service=route.service_name if route else None,
            boosts=self.learner.retrieval_boosts(),
            # P2.3: restrict to relevant collections; None = search all
            collections=_intent_collections(route) or (
                "semantic", "code", "docs", "architecture", "defects", "incidents"
            ),
        )
        # P2.2: apply intent-aware graph hop depth for this retrieval
        if route:
            self.retriever._graph_hops = _intent_graph_hops(route.intent,
                                                             self.retriever._graph_hops)

        pack = build_evidence_pack(
            question, retrieval,
            graph_nodes=retrieval.graph_nodes,
            query_tokens=understood.tokens,
            intent=route.intent if route else "general_search",
            handler=route.handler if route else "search_code",
        )
        web_used = False
        if should_use_web(
            enabled=self.settings.web_search_enabled,
            confidence=pack.confidence,
            min_confidence=self.settings.web_search_min_confidence,
        ):
            try:
                web_hits = search_web(question, self.settings, limit=5)
                if web_hits:
                    pack = pack.with_web(web_hits)
                    web_used = True
            except Exception as exc:
                log.warning("ask_web_search_failed", error=str(exc))

        polished = synthesize_from_pack(self.llm, pack)
        header = (
            f"_Intent: `{pack.intent}` → `{pack.handler}` "
            f"(pack confidence {pack.confidence:.0%}"
            f"{'; web gap-fill' if web_used else ''})_\n\n"
        )
        citations = citations_from(retrieval.chunks)
        if web_used:
            for w in pack.web_facts:
                citations.append({"path": w.source, "text": w.text[:80], "kind": "web"})
        return self._ask_response(
            question=question, expanded=query, route=route, pack=pack,
            understood=understood, markdown=header + polished,
            citations=citations,
            extra_data={"web_used": web_used},
            summary=(f"{len(pack.facts)} fact(s), confidence {pack.confidence:.0%}"
                     + (", web gap-fill" if web_used else "")),
            chunk_ids=[rc.chunk.id for rc in retrieval.chunks],
            paths=[rc.chunk.rel_path for rc in retrieval.chunks],
            repos=[rc.chunk.repo for rc in retrieval.chunks],
            web_used=web_used,
        )
    def _ask_response(
        self, *, question, expanded, route, pack, understood, markdown, citations,
        extra_data, summary, chunk_ids, paths, repos=None, web_used=False,
    ) -> ToolResponse:
        entities = [n for n, _ in (understood.linked_entities or [])]
        if route and route.service_name:
            entities.append(route.service_name)
        event = self.learner.record_ask(
            question=question, expanded=expanded,
            intent=pack.intent if pack else (route.intent if route else ""),
            handler=pack.handler if pack else (route.handler if route else ""),
            confidence=pack.confidence if pack else (route.confidence if route else 0.0),
            chunk_ids=chunk_ids, paths=paths, entities=entities,
        )
        # P4.2: log to analytics store
        self._record_analytics(
            tool="ask",
            intent=pack.intent if pack else (route.intent if route else ""),
            handler=pack.handler if pack else (route.handler if route else ""),
            query=question,
            latency_ms=0,  # timing provided by observability wrapper
            chunks=len(chunk_ids),
            llm_called=bool(pack and pack.llm_used if hasattr(pack, "llm_used") else False),
            interaction_id=event.id,
        )
        footer = (
            f"\n\n_interaction `{event.id}` — if this helped, "
            f"`rate_answer(\"{event.id}\", 5)`; if not, rate 1–2 so retrieval learns._"
        )
        from ...nlp.freshness import compute_freshness, freshness_markdown
        freshness = compute_freshness(
            repos=repos or [], web_used=web_used, metadata=self._meta,
        )
        data = {
            "intent": pack.intent if pack else None,
            "routed_to": pack.handler if pack else None,
            "confidence": pack.confidence if pack else None,
            "interaction_id": event.id,
            "nlp": {"tokens": understood.tokens,
                    "entities": understood.linked_entities,
                    "ac_clauses": getattr(understood, "ac_clauses", [])},
            "pack": pack.model_dump() if pack else {},
            **extra_data,
        }
        return ToolResponse(
            tool="ask",
            query={"question": question, "expanded": expanded,
                   "intent": data["intent"], "routed_to": data["routed_to"],
                   "interaction_id": event.id},
            summary=summary,
            data=data,
            markdown=markdown + freshness_markdown(freshness) + footer,
            citations=citations,
            freshness=freshness,
        )
    def _record_analytics(self, tool: str, intent: str, handler: str,
                          query: str, latency_ms: int, chunks: int,
                          llm_called: bool, interaction_id: str) -> None:
        """P4.2: fire-and-forget analytics record; never raises."""
        try:
            from ...db.analytics_store import QueryRecord
            self._analytics.record(QueryRecord(
                tool=tool, intent=intent, handler=handler,
                query_text=query, latency_ms=latency_ms,
                chunks_used=chunks, llm_called=llm_called,
                interaction_id=interaction_id,
            ))
        except Exception:
            pass
    def rate_answer(self, interaction_id: str = "last", rating: int = 5,
                    note: str = "") -> ToolResponse:
        """Teach the knowledge base: 5 = useful (boost those passages), 1 = not useful."""
        event = self.learner.rate(interaction_id, rating, note)
        if event is None:
            return ToolResponse(
                tool="rate_answer",
                query={"interaction_id": interaction_id, "rating": rating},
                summary="No matching interaction to rate.",
                markdown="No matching interaction. Ask a question first, or pass a valid interaction id.",
            )
        # P4.1+P4.2: persist rating back to analytics so SQL queries can filter by quality
        self._analytics.record_rating(event.id, rating)
        label = "useful" if rating >= 4 else ("not useful" if rating <= 2 else "mixed")
        return ToolResponse(
            tool="rate_answer",
            query={"interaction_id": event.id, "rating": rating},
            summary=f"Recorded {label} rating for `{event.id}`.",
            data={"event": event.model_dump(), "learning": self.learner.summary()},
            markdown=(
                f"Recorded **{rating}/5** ({label}) for interaction `{event.id}`.\n\n"
                f"Learned aliases: {self.learner.state().n_interactions} interactions, "
                f"{len(self.learner.state().aliases)} aliases, "
                f"{len(self.learner.state().chunk_boosts)} boosted chunks.\n"
            ),
        )
    def learning_status(self) -> ToolResponse:
        """Show what the online learner has absorbed from usage."""
        summary = self.learner.summary()
        lines = [
            "# Learning status",
            f"- Enabled: {summary['enabled']}",
            f"- Interactions: {summary['n_interactions']}",
            f"- Explicit ratings: {summary['n_ratings']}",
            f"- Implicit + / −: {summary['n_implicit_pos']} / {summary['n_implicit_neg']}",
            f"- Aliases: {summary['alias_count']}",
            f"- Boosted chunks: {summary['boosted_chunks']}",
            "",
            "## Top aliases",
        ]
        for row in summary["top_aliases"] or ["_(none yet)_"]:
            if isinstance(row, dict):
                lines.append(f"- `{row['pair']}` ×{row['count']}")
            else:
                lines.append(f"- {row}")
        lines += ["", "## Top retrieval boosts"]
        for row in summary["top_chunk_boosts"] or []:
            lines.append(f"- `{row['id']}` boost={row['boost']}")
        if not summary["top_chunk_boosts"]:
            lines.append("_(none yet — rate answers 4–5 to grow this)_")
        return ToolResponse(
            tool="learning_status", query={},
            summary=(f"{summary['n_interactions']} interactions, "
                     f"{summary['alias_count']} aliases, "
                     f"{summary['boosted_chunks']} boosted chunks."),
            data=summary, markdown="\n".join(lines) + "\n",
        )
    def _ask_entity_names(self) -> list[str]:
        names: list[str] = []
        try:
            for nt in (NodeType.CONTROLLER, NodeType.ENDPOINT, NodeType.ENTITY,
                       NodeType.SERVICE_LAYER):
                for n in self.graph.nodes_by_type(nt)[:80]:
                    if n.get("name"):
                        names.append(n["name"])
        except Exception:
            pass
        return names[:250]
    def _dispatch_ask(self, route, question: str) -> ToolResponse:
        handler = route.handler
        svc = route.service_name
        try:
            if handler == "get_defect_changes" and route.ticket_id:
                return self.get_defect_changes(route.ticket_id)
            if handler == "explain_service" and svc:
                return self.explain_service(svc)
            if handler == "explain_architecture":
                return self.explain_architecture()
            if handler == "inspect_application":
                return self.inspect_application()
            if handler == "inspect_service":
                return self.inspect_service(svc or question)
            if handler == "find_endpoint":
                return self.find_endpoint(route.endpoint_path or question)
            if handler == "trace_business_flow":
                return self.trace_business_flow(question)
            if handler == "impact_analysis":
                return self.impact_analysis(svc or question)
            if handler == "analyze_defect":
                return self.analyze_defect(question)
            if handler == "implement_feature":
                return self.implement_feature(question)
            if handler == "onboarding_assistant":
                return self.onboarding_assistant(svc or question)
            if handler == "security_audit" and svc:
                return self.security_audit(svc)
            if handler == "kafka_topology":
                return self.kafka_topology()
            return self.search_code(question, svc)
        except Exception as exc:
            log.warning("ask_dispatch_failed", handler=handler, error=str(exc))
            return self.search_code(question, svc)
    def _resolve_method_node(self, class_name: str, method_name: str) -> str | None:
        matches = self.graph.find_nodes(method_name, {NodeType.METHOD}, limit=25)
        for m in matches:
            if m.get("attributes", {}).get("class", "").lower() == class_name.lower():
                return m["id"]
        if matches:
            return matches[0]["id"]
        endpoint_matches = self.graph.find_nodes(method_name, {NodeType.ENDPOINT}, limit=5)
        return endpoint_matches[0]["id"] if endpoint_matches else None
