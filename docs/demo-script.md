# Demo video script (about 4 minutes)

Record at 1920x1080. Two windows: a terminal and the dashboard (`python3 -m http.server -d ui 8765`, then http://localhost:8765). Before recording, run one case once so Savanna is awake (Auto Resume can take a minute on the first query).

## 0:00–0:25 · The problem

**Screen:** the dashboard's case list.

> Fraud analysts spend most of an alert gathering context: the card's history, the device, the region, earlier cases, the policy. This agent does that on a TigerGraph knowledge graph, asks for more evidence only when the answer could change, and ties every decision to a graph query and a policy rule. These are the 20 benchmark cases from November and December 2016.

## 0:25–1:30 · One case live, end to end

**Screen:** terminal.

```
LIVE=1 USE_LLM=1 .venv/bin/python agent/agent.py HHG-011
```

Let the numbered steps scroll. Narrate over them:

> HHG-011 is a customer report: "I never made this $131.30 purchase." The agent opens a case and reads the card's baseline from the graph. Every call goes through the TigerGraph MCP server as an installed GSQL query, with `as_of` set to when the case opened, so the agent can't see the future.
>
> It runs the pattern detectors, then walks from the transaction to its device profile and out to other cards. That device was used on two other cards in the previous five hours, for $125.72 and $125.59, and both of those score as suspicious.
>
> It checks case memory: closed cases on this card, closed cases that used the same device, and cases it wrote itself earlier in the run.
>
> The customer's denial plus the shared device puts the probability past the stopping threshold, so it stops there. It recommends a card block (team lead approval), a case, a suspicious activity report because the device is shared (rule R6), and monitoring of the connected cards. The case is written back into the graph.

## 1:30–2:30 · The dashboard

**Screen:** dashboard, open HHG-011.

- The probability bar from before to after the evidence.
- The initial and final actions, each with its approval route. Click **approve** on the `L1` block: only `auto` actions execute on their own.
- The timeline: each step shows the tool calls behind it.
- The evidence list: each item cites a query or a document chunk.
- The graph neighbourhood: the card, the shared device, the two other cards.
- The SAR: who, what, when, where, how and why, following FinCEN's guidance, which was retrieved from the graph.

## 2:30–3:15 · Uncertainty and asking for evidence

**Screen:** dashboard, open HHG-004.

> Not every case is clear. HHG-004 is a customer denial, but the graph evidence is weak: the scorer trained on 5,565 closed cases puts it at 0.01. The agent first asks the customer to validate the charge and states the reply it assumed. The customer denies it again, so rule R2 calls for a block. The graph still disagrees, so the verdict stays uncertain and the case goes to an analyst under R8. The before and after recommendations are both recorded, with what changed between them.

Then open a legitimate risk-score case (for example HHG-010):

> Most alerts above 0.7 turn out to be legitimate. Here two independent signals say legitimate, so the §6 stopping rule closes it without bothering the customer.

## 3:15–3:50 · What the graph found

**Screen:** dashboard, HHG-014, then HHG-006.

> Two patterns aren't among the five documented ones. A device ring: one Android profile behind an anonymous proxy, always new to the account, one to three purchases per card across 24 cards in August and September, then 28 more from mid-November. HHG-014 is one of them; a connected-components query over the card–device graph returns a 19-card component joined by that one device. And structuring: four purchases just under $500 within thirty minutes in HHG-006. Both are filed under R9 as undocumented, described in the agent's own words.

## 3:50–4:10 · Close

**Screen:** README architecture block.

> The LLM explains; it doesn't decide. Probabilities come from the scorer and evidence weights, actions and routes from policy code that lints every answer. The graph holds the data, the policy, the documents and the agent's own memory. Code and all 20 answer files are on GitHub.
