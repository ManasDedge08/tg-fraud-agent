# Social post drafts

Replace `<BLOG_URL>` and `<REPO_URL>` before posting.

## LinkedIn

We built a fraud investigation agent on @TigerGraphDB for the Hacker House Goa 2026 challenge.

It takes an alert (a model score, a customer complaint or an analyst request), investigates it on a knowledge graph through TigerGraph MCP, and recommends what the bank should do and who has to approve it.

What worked:

• The LLM explains; it doesn't decide. Probabilities come from a scorer trained on 5,565 closed cases, and actions come from policy code that refuses to emit an answer that breaks policy.
• The agent knows when to stop. Two independent signals either way and it acts; otherwise it asks the customer and records how the recommendation changed.
• The graph found patterns no single card shows. One device profile behind an anonymous proxy hit 24 cards in August and September, then 28 more in November. A connected-components query over the card–device graph picks it out.
• GraphRAG over the fraud policy, the known patterns, FinCEN's SAR guidance and every closed-case note, stored as vectors next to the data they describe.
• Case memory: every investigation is written back to the graph, where the next one can find it.

Write-up: <BLOG_URL>
Code: <REPO_URL>

#TigerGraph #GraphRAG #FraudDetection #AIAgents #HackerHouseGoa

## X

Built a fraud investigation agent on @TigerGraphDB for Hacker House Goa: TigerGraph MCP, GraphRAG over policy + FinCEN guidance, case memory written back to the graph. The LLM explains, policy code decides. It found a device ring across 52 cards. <BLOG_URL>
