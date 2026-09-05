# Sample prompts

The default MCP tool is **`ask`**. Type a normal question; the server classifies
it and answers from the project knowledge graph plus technology concepts.

> "How does eligibility checking work?"
> "What is `@Transactional`?"
> "Review DE194624"
> "What depends on rx-order-service?"

Calls `ask("<the full user message>")`. Specialized tools remain available for
narrow follow-ups; slash prompts take **no extra fields** — they apply to the
current chat message.

These illustrate how an agent/IDE would call the nine tools. Each tool returns a
JSON envelope: `{ tool, query, summary, data, citations, markdown, generated_at }`.

## explain_service
> "Give me an overview of **rx-order-service** — its endpoints, tables and who it
> depends on."

Calls `explain_service("rx-order-service")` →
`data.endpoints`, `data.tables`, `data.depends_on`, `data.kafka_producers`.

## find_endpoint
> "Where is the endpoint that **creates an order**?"
> "Find anything under **/api/orders**."

Calls `find_endpoint("create order")` / `find_endpoint("/api/orders")` →
`data.matches[*].{http_method,path,controller,handler,file}`.

## trace_business_flow
> "Trace the **order fulfilment** flow from checkout to shipment."

Calls `trace_business_flow("order fulfilment")` →
`data.participating_services`, `data.endpoints`, `data.events`,
`data.service_dependencies`.

## impact_analysis
> "If I add a column to the **orders** table, what breaks?"
> "What is the blast radius of changing the **Order** entity?"

Calls `impact_analysis("orders")` →
`data.impacted_services`, `data.total_impacted_nodes`, `data.levels` (per hop).

## analyze_defect
> "Orders are stuck in **PENDING** after the last deploy; payments succeed but the
> order never moves to PAID. What's the root cause?"

Calls `analyze_defect("orders stuck in PENDING, payment.completed not consumed")` →
`data.suspect_services`, `data.impacted`, grounded narrative in `markdown`.

## implement_feature
> "I need to add **partial refunds** to orders. What services, endpoints, DTOs,
> tables and events are involved and in what order?"

Calls `implement_feature("add partial refunds to orders")` →
`data.touchpoints.{services,endpoints,entities}`, step-by-step `markdown`.

## search_domain_knowledge
> "How does the platform handle **eligibility checks**?"
> "Show me docs and incidents about **Kafka consumer lag**."

Calls `search_domain_knowledge("eligibility checks")` →
`data.results[*].{path,excerpt,score}`, `data.services`, `data.graph_nodes`.

## generate_architecture_summary
> "Give me a **whole-system architecture summary** with a dependency diagram."

Calls `generate_architecture_summary()` →
`data.services`, `data.service_dependencies`, `data.event_topics`,
`data.mermaid` (paste into any Mermaid renderer).

## onboarding_assistant
> "I'm new to the team and will work on **payments**. Where do I start?"

Calls `onboarding_assistant("payments")` →
`data.steps`, `data.suggested_services`, `data.reading_list`.

---

### Tips
- Add `LLM_PROVIDER=openai` + `OPENAI_API_KEY` for narrative synthesis; without an
  LLM the tools still return accurate, citation-backed extractive answers.
- Prefer the structured `data` field for programmatic chaining; use `markdown` for
  display.
- Re-run `mcp-kb-refresh` after pulling new commits so answers reflect the latest code.
