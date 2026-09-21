# An agent that knows when to stop: fraud investigation on TigerGraph

Fraud analysts spend most of an alert pulling context together: the cardholder's history, the device, the region, earlier cases, the policy. Then they decide whether to block, verify or let it go. For the TigerGraph × Hacker House Goa 2026 challenge we built an agent that does that work on a knowledge graph, asks for more evidence only when the answer could change, and leaves every decision traceable to a graph query and a policy rule.

## What we built

The agent takes one of 20 alerts from November and December 2016: a model score, a customer complaint, or an analyst request. For each one it:

1. opens a case and reads the card's baseline from the graph;
2. runs pattern queries: card testing, bursts, new devices, out-of-region use, recurring charges, and cross-card traversals over devices and regions;
3. checks case memory: closed cases on this card, closed cases that used the same device, and cases the agent itself wrote earlier in the run;
4. keeps a hypothesis board in log-odds, one line per signal, and turns it into a fraud probability;
5. stops if the probability is at or above 0.85 or at or below 0.15 with two independent signals behind it; otherwise it asks the customer or requests step-up authentication;
6. recommends actions under the bank's policy, with the approval route (`auto`, `L1`, `L2`), before and after the evidence;
7. writes a suspicious activity report when policy requires one;
8. writes the case back into TigerGraph as a `FraudCase` vertex, where the next investigation can find it.

A dashboard shows each case: the probability moving from before to after the evidence, both sets of actions with their routes and approve buttons for the `L1`/`L2` ones, the investigation timeline, every evidence item with its query reference, the graph neighbourhood, and the SAR.

## Architecture

```
case_pack row ─► Investigation
                   ├─ TigerGraph MCP ─► installed GSQL: similar_closed_cases, closed_cases_by_device,
                   │                    device_neighbors, region_cluster, policy_for_pattern, device_rings
                   ├─ scorer (trained on closed-case outcomes, calibrated on October)
                   ├─ detectors + hypothesis board
                   ├─ evidence request (simulated reply, stated in the case file)
                   ├─ policy.py: actions, routes, SAR rule, lint
                   ├─ GraphRAG: vector search over policy, patterns, FinCEN guidance, closed-case notes
                   │            + graph path Pattern → PolicyRule → DocChunk
                   ├─ LLM: summary and SAR narrative from facts + retrieved passages
                   └─ write-back via MCP: FraudCase + edges
```

One rule shaped the design: **the LLM explains, it does not decide.** Probabilities come from the scorer and the evidence weights; actions, routes and rule citations come from `policy.py`, which also lints every answer and refuses to emit one that breaks policy (a block on a single weak signal, a SAR without `FILE_REPORT`, a route that doesn't match exposure). The LLM gets a compact JSON of facts and writes the analyst summary and the regulator-facing narrative. Nothing it writes can change what the bank does.

## How TigerGraph is used

**Schema.** The suggested Customer, BankCard, Txn, DeviceProfile, EmailDomain, BillingRegion and ClosedCase, with `NEXT_TXN` edges in time order. Two additions:

- `FraudCase`, the agent's own cases, linked to the card, the affected transactions, the device, connected cards, the pattern and the closed cases it drew on. That is the memory: `similar_closed_cases` returns closed cases *and* agent cases for a card.
- The policy as a graph: `Pattern → TRIGGERS → PolicyRule → REQUIRES → PolicyAction`. One traversal returns which rules and actions apply once a pattern is identified.

**Queries.** Every query takes `as_of` and ignores anything after the case opened, so the agent can't peek at the future. `closed_cases_by_device` walks DeviceProfile → Txn → ClosedCase, which is how a device seen in August fraud becomes evidence in November. `device_rings` runs connected components over the card–device graph, restricted to devices marked New and behind a proxy.

**GraphRAG.** The policy, the five known patterns and FinCEN's SAR narrative guidance are chunked into `DocChunk` vertices with vector embeddings, and every closed case's analyst notes get a vector too. Policy chunks are wired to `PolicyRule` and `Pattern` vertices. For each case the agent writes a query from its own findings and retrieves in two ways: by vector similarity, and by walking the graph from the pattern it identified to the rules and their text. The LLM receives the facts plus those passages, never raw rows, which is why the SAR narratives follow FinCEN's who/what/when/where/why/how and cite the right rule numbers.

**MCP.** At run time the agent reaches the graph only through the TigerGraph MCP server: `tigergraph__run_installed_query` and `tigergraph__search_top_k_similarity` for investigation and retrieval and `tigergraph__add_node` / `tigergraph__add_edges` for write-back. Bulk loading of 590,742 transactions went through pyTigerGraph.

## What the graph found

Two patterns in the data aren't among the five documented ones, and one case can only be solved by looking at other cards.

- **A device ring.** One Android device profile, always behind an anonymous proxy and always "New" to the account, made one to three purchases of $35–$250 on 24 cards in August and September; analysts closed four of those as fraud without a matching typology. From 14 November the same profile appears on 28 more cards. HHG-014 is one of them. No single card looks alarming; the device's neighbourhood does.
- **Structuring under $500.** Four online purchases within 30–40 minutes, each just under $500, on one card. Five September closed cases share the shape. HHG-006 is $478.95, $456.96, $488.04 and $482.12 in thirty minutes, from two devices new to the card.
- **A shared device.** In HHG-011 the disputed $131.30 purchase came from a device used on two other cards in the previous five hours for $125.72 and $125.59, both of which score as suspicious. That link is what turns a single disputed charge into a report under rule R6.

## Agentic capabilities

- **Knowing when to stop.** The §6 stopping rule is code, and the dashboard shows why each case stopped. Many high-score alerts close without bothering the customer, because two independent signals already say legitimate.
- **Asking only when it matters.** If the probability is in the uncertain band, the agent asks the customer (and requests step-up for online charges), states the simulated reply, and shows how the recommendation changed.
- **Permissions.** Only `auto` actions execute. `L1` and `L2` actions are recommended with their route and wait for a human; the dashboard's approve buttons stand in for that queue.
- **Memory that grows during the run.** Cases are processed in `opened_at` order and written back as they close, so a later case can retrieve an earlier one.

## What we learned

- The bank's risk score is a weak guide in this data: a scorer trained on the closed cases reached AUC 0.91 on October against 0.87 for the risk score, and most alerts above 0.7 were legitimate.
- The dataset hides structure in its IDs. `card_id` isn't a column; it's customer plus the rank of (network, card type), which we checked against every closed case.
- Putting the policy in code, not in the prompt, removed a whole class of errors: wrong routes, missing cases behind reports, blocks on a single weak signal.

## With more time

- Replace the single-reply simulation with a branch plan per case (deny → R2, confirm → R3, no reply → R4) and pick the question whose answer would change the actions most.
- Add graph embeddings (FastRP) of each case's neighbourhood so similar-case retrieval matches on structure as well as on text and shared entities, and add the FATF typology reports to the document store next to FinCEN's guidance.
- Run the agent continuously over the exam period, picking up alerts from the risk scores beyond the 20 cases.
- Back-test the whole loop on October closed cases and publish the calibration curve.
