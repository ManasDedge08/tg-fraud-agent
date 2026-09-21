# Fraud investigation agent on TigerGraph

An agent that takes a fraud alert (risk score, customer report or analyst request), investigates it on a TigerGraph knowledge graph through TigerGraph MCP, decides what kind of fraud it is and how far it goes, asks for more evidence when it is unsure, recommends policy-valid next actions with the right approval route, writes a SAR when policy requires one, and stores the case back in the graph as memory.

Built for the TigerGraph × Hacker House Goa 2026 fraud task. Output for the 20 benchmark cases is in [`cases/`](cases/); the step-by-step trace of each investigation is in [`traces/`](traces/).

## How it works

```
trigger (case_pack row)
  └─ Investigation
       ├─ graph tools ── TigerGraph MCP server → installed GSQL queries (gsql/queries.gsql)
       │                 + local DuckDB mirror for bulk feature reads
       ├─ scorer ─────── gradient boosting on 5,565 closed-case outcomes, isotonic-calibrated
       ├─ detectors ──── card testing, just-under-$500 structuring, device ring, shared device,
       │                 new device, out-of-region vs trip, recurring charge, match flags
       ├─ hypotheses ─── log-odds board: every signal adds or removes weight
       ├─ stop or ask ── §6 stopping rule; otherwise customer validation / step-up (simulated reply)
       ├─ policy.py ──── actions, routes, SAR decision; lint() blocks any policy-invalid answer
       ├─ graphrag.py ── vector search (MCP) over policy, patterns, FinCEN guidance and
       │                 closed-case notes + graph path Pattern → PolicyRule → DocChunk
       ├─ explain.py ─── LLM writes summary + SAR narrative from facts + retrieved passages
       └─ memory ─────── FraudCase vertex + edges written back through MCP
  → cases/HHG-0xx.json, traces/HHG-0xx.json, ui/
```

The LLM explains; it does not decide. Probabilities come from the scorer and the evidence weights, actions and routes from `policy.py`. That keeps rule citations correct and results repeatable across all 20 cases.

## What the graph found

- **Device ring (undocumented, R6/R9).** One device profile, `SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080`, always behind an anonymous proxy and always marked New, makes one to three mid-sized online purchases per card and moves on. 24 cards in Aug–Sep (closed cases CC-2649, CC-2971, CC-2985, CC-3035), then 28 more cards from 14 Nov. HHG-014 is one of them. Found by traversing Txn → DeviceProfile → other cards and matching against closed-case memory.
- **Structuring (undocumented, R9).** Several online purchases on one card within an hour, each just under $500. Five closed cases in September share the shape. HHG-006 is four purchases of $457–$488 within 30 minutes.
- **Shared device (R6).** HHG-011: a device profile used on three different cards within five hours for $125–$131 each; the other two cards' transactions score as suspicious.

## Graph

Schema in [`gsql/schema.gsql`](gsql/schema.gsql): the suggested Customer, BankCard, Txn, DeviceProfile, EmailDomain, BillingRegion, ClosedCase, plus

- `FraudCase` — the agent's own cases, linked to card, transactions, devices, connected cards, pattern and similar closed cases. Later investigations find them through `similar_closed_cases`.
- `Pattern → TRIGGERS → PolicyRule → REQUIRES → PolicyAction` — the fraud policy as a graph, so one traversal returns the rules and allowed actions for a pattern.

## GraphRAG

Text lives in the graph next to the data it explains ([`agent/graphrag.py`](agent/graphrag.py)):

- `DocChunk` vertices with a 384-d vector attribute (`BAAI/bge-small-en-v1.5`): each policy rule and section, the five known patterns, the dataset guide, and FinCEN's SAR narrative guidance (98 chunks). Policy chunks link to `PolicyRule` and `Pattern` vertices.
- `ClosedCase.note_emb`: all 5,565 closed-case analyst notes as vectors, so case memory can be searched by meaning as well as by shared card or device.

For each case the agent builds a query from its findings, then through MCP: `search_top_k_similarity` over `DocChunk`, the installed query `policy_docs` (Pattern → PolicyRule → DocChunk) for the pattern it identified, and `search_top_k_similarity` over closed-case notes. Retrieved passages become `document` evidence citing the chunk ID, and they are passed to the LLM with the facts, so SAR narratives follow FinCEN's who/what/when/where/why/how and cite the rules that apply.

Queries in [`gsql/queries.gsql`](gsql/queries.gsql): `card_window`, `card_history`, `device_neighbors`, `closed_cases_by_device`, `similar_closed_cases`, `region_cluster`, `policy_for_pattern`, and `device_rings`: connected components (label propagation) over the card–device graph restricted to New devices behind a proxy. The agent runs it through MCP for online cases from a New device behind a proxy; for HHG-014 it returns a 19-card component joined by one device, which confirms the ring without relying on the closed-case match.

Every query takes `as_of` and ignores anything after the case opened.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install duckdb pandas scikit-learn pyTigerGraph python-dotenv tigergraph-mcp mcp gdown fastembed pypdf
.venv/bin/gdown --folder <dataset folder> -O data/       # HHGOA_IEEE
.venv/bin/python agent/build_mirror.py                     # DuckDB mirror + derived card_id
.venv/bin/python agent/train_scorer.py
cp .env.example .env                                      # TG_HOST, TG_SECRET, TG_GRAPHNAME=Fraud, TG_TGCLOUD=true
.venv/bin/python agent/load_graph.py all                  # schema, data, queries
.venv/bin/python agent/graphrag.py build                  # DocChunk + note vectors, policy_docs query
USE_LLM=1 .venv/bin/python agent/agent.py                 # all 20 cases, in opened_at order
.venv/bin/python ui/build.py && python3 -m http.server -d ui 8765
```

`OFFLINE=1` runs without TigerGraph (cases are then marked `written_to_graph: false`).

## Evidence simulation

Customer and analyst replies are not in the dataset. When the agent asks, the reply is simulated to be consistent with the graph evidence excluding the bank's risk score: probability ≥ 0.5 → the customer denies, otherwise confirms. The assumption is written into `evidence_requests` of every case.

## Limits

- The scorer is trained on positive-unlabeled data (uninvestigated transactions count as legitimate), so raw probabilities run low; the calibration is on October.
- `card_id` is not a column in the data; it is derived as customer + rank of (card network, card type), which matches 100% of closed-case card IDs.
