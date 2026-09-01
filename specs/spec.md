---
id: traitors-mobile-mode-b-sim-spec
type: spec
project: traitors-mobile
parents: []
status: draft
version: 1
paperclip_issue: SWA-145
owner_role: Designer
created: 2026-09-01
updated: 2026-09-01
---

# Spec: Mode B (The Parlour) AI Agent Simulation — Phase 1

## 1. Problem / purpose

Build a turn-based text discussion among LLM-backed player agents that simulates the "Mode B / The Parlour" social-deduction mechanic from the Traitors Mobile design, and use it to produce real, measured data on whether the mechanic works:

- Does the group correctly identify the Traitor at a plausible, tunable rate?
- Does the discussion show real inference (reasoning that goes beyond a player's own observation cards) rather than pure fact-sharing?

This is Experiment 1 ("baseline catch rate") from `AI_Simulation_Design.md`. Experiments 2–5 (information density, Detective impact, noise, model comparison) are explicitly deferred to a follow-up brief.

Source grounding: this spec is derived from (not copied verbatim from) the six source documents — `Family_Prototype_GM_Pack.md` (clearest concrete description of the mechanic), `AI_Simulation_Design.md` (prior simulation sketch, treated as hypothesis not authority), `Mode_B_Conversation_Examples.md` (conversation mechanic), `pitch_doc_v1.md` (framing only), `Role_Analysis_Source_Games.md` (role design rationale), `The_Games.md` (why async text-based).

## 2. What the thing does (functional overview)

1. **Scenario generation** — produces at least one concrete scenario (baseline: the stolen prize tin, below) with a fixed cast and per-player private role material.
2. **Discussion run** — an orchestrator drives a discussion among 5 LLM-backed player agents using structured turn actions. Each player acts from their private role material plus the shared conversation transcript; the orchestrator manages turn order, enforces session rules, and records every exchange.
3. **Accusation tracking** — formal accusations are detected, logged, and attributed (accuser, target, reason, turn).
4. **Final vote** — after the discussion schedule, each player submits a private final vote; the orchestrator tallies votes and determines whether the Traitor was caught.
5. **Logging** — a full structured transcript per game (not just console output), plus per-game result records.
6. **Metrics** — a batch mode runs multiple games (baseline ≥ 10) and reports catch rate, average exchanges before resolution, and formal-accusation usage.

## 3. Cast (confirmed from source)

**5 players: 1 Traitor, 1 Detective, 3 Loyalists.** This is exactly the baseline cast of the Family Prototype GM Pack (the clearest concrete description of the mechanic), so it is confirmed rather than changed. Note: `Mode_B_Conversation_Examples.md` shows 8-player tables with 2 Traitors — that is the multi-Traitor variant; Phase 1 keeps the single-Traitor 5-player baseline so catch-rate results attribute cleanly to the mechanic.

Household identities (from the source material):

| Household | Role |
|-----------|------|
| The Abbotts | Traitor |
| The Murphys | Detective |
| The Chens | Loyalist (A) |
| The Patels | Loyalist (B) |
| The Okayes | Loyalist (C) |

## 4. Role cards (private material)

Each player receives private material: a role, a goal, and private observation cards. The Traitor additionally receives a sealed crime declaration and a cover story. The Detective additionally receives a directional hint. Content is equivalent to the GM Pack cards — the simulation should reproduce the same facts (times, places, sightings), not copy the prose verbatim.

**The Traitor (The Abbotts)**
- Goal: avoid being formally accused; redirect suspicion; may lie; may share or withhold cards selectively (never reveal the crime declaration).
- Private observations:
  - Signed the visitor book on arrival at 7:10pm — earlier than most people.
  - Noticed the office door was unlocked at ~8:40pm while walking past on the way to the toilets.
  - When re-entering the bar at 8:57pm, noticed The Patels (Loyalist B) looking in their direction.
- Sealed crime declaration: slipped away from the bar at 8:45pm, told no one, went to the office, found it unlocked, took the tin, hid it in their bag, back at the bar by 8:57pm (just before the 9:00pm toast).
- Cover story: at the bar the entire evening from 8:20pm; bought a round of drinks at ~8:30pm; never left the bar area.

**The Detective (The Murphys)**
- Goal: help identify the thief; share observations strategically; corroborate others; watch for inconsistencies between claims and known facts.
- Private observations:
  - At the buffet table 8:30pm–9:05pm with a clear sightline to the corridor leading to the office.
  - Between 8:40pm and 9:00pm, two separate people left the main hall toward the corridor; couldn't clearly identify the second; the first was The Chens (Loyalist A).
  - Spoke to the caretaker at 9:10pm; he mentioned someone had asked him about the heating earlier — odd, because the heating was fine all night.
- Directional hint (private): believes the theft occurred between 8:40pm and 8:55pm.

**Loyalist A (The Chens)**
- Goal: help the group; share observations honestly; be sceptical of claims that don't add up.
- Private observations:
  - Talking to the event organiser near the stage 8:15pm–8:50pm.
  - At ~8:40pm noticed the office corridor door ajar; assumed the caretaker.
  - Walked through the corridor at 8:50pm — office door closed by then.
  - Passed someone in the corridor at ~8:55pm heading toward the main hall; no clear look.

**Loyalist B (The Patels)**
- Goal: help the group; pay attention to what doesn't add up.
- Private observations:
  - At the bar from 8:20pm until the 9:00pm toast.
  - Fairly confident The Abbotts (Traitor) were at the bar earlier, but absent from ~8:45pm until just before the toast.
  - Saw the caretaker near the office corridor at ~8:35pm, then not again until after the toast.
  - When The Abbotts returned just before the toast, they seemed slightly flustered (could be misremembering).

**Loyalist C (The Okayes)**
- Goal: help the group; ask good questions and apply pressure; has less information than others.
- Private observations:
  - Went to the cloakroom (far end of the office corridor) at 8:58pm.
  - Passing the office at 8:58pm, door closed, nothing unusual.
  - Came back through the corridor at 9:03pm — office door now open, light on; assumed someone working late.
  - No strong observations about who was where.

Card-parameterisation requirement: Loyalist B's card references "The Abbotts" by name — the scenario generator must substitute the actual assigned player identity so the card is concrete per game.

## 5. Scenario (baseline): the stolen prize tin

Meadowbrook Community Summer Fair, Saturday evening. The prize fund — £340 in a locked tin — was confirmed present and locked at 7:30pm by two committee members. At 9:15pm, event organiser Janet Marsh discovered it missing from the community centre office. The office was left unlocked during the event, as normal when the building is staffed. Everyone in the group was present during the relevant window (7:30pm–9:15pm). At least one person in the group stole the tin; it may have been done alone.

Crime window: 8:40pm–8:55pm. Traitor's sealed crime: absent from the bar 8:45pm–8:57pm; office unlocked; tin taken and hidden in bag.

The scenario generator must produce at least this one concrete scenario. Parameterising it further (names, times, money amounts, additional scenarios) is a bonus, not required.

## 6. Structured turn actions

Six action types, matching the mechanic:

| Action | Behaviour |
|--------|-----------|
| **Statement** | Claim something about your actions or observations, with specifics of time/location where relevant. |
| **Question** | Ask ONE direct question to a specific named player, format "[Player]: [question]". The target must respond. |
| **Challenge** | Dispute a specific claim another player made; reference the exact statement being disputed. |
| **Corroboration** | Support another player's statement if you observed the same thing (the source docs call this "alibi"). |
| **Formal accusation** | "I formally accuse [name] because [reason]." Requires a reason. |
| **Final vote** | Private vote: a player name, or "no accusation". |

Turn model: opening prompt → a fixed schedule of rounds with a defined action mix (derived from the source sketch: opening statements → interrogation (question/challenge/corroboration) → pressure → accusation window → closing) → private final votes. Speaking order varies per round. Baseline schedule is 6 rounds ending in the vote phase (a tunable constant, not a hardcoded invariant).

Session rules the orchestrator must enforce:
1. Players may share or withhold their private observations, but never claim observations they were not given (no fabrication).
2. A player asked a direct question must respond — they may be vague or deflect, but not silent.
3. No out-of-character chat; stay in character.
4. Players do not announce their role ("As a Traitor…" is forbidden).
5. The Traitor may lie; everyone else is truthful about their observations (but may be selective).

## 7. LLM backend requirements

- **Claude API is the primary, reliable backend and the default in configuration.** All normal runs (including the ≥10-game baseline batch) use it.
- **Local Ollama is a secondary, opportunistic backend only.** There is no GPU and no Ollama on this machine; a local model is reachable only via Ollama on Chris's desktop over the LAN (`http://192.168.0.38:11434`, native and OpenAI-compatible `/v1` APIs), and only while that machine happens to be on with Ollama started. It is not an always-on inference server and must never be treated as a reliable default.
- Backend and model selection must be **configuration-driven** (per run), with Claude as the shipped default. The design must make the local backend's opportunistic nature explicit (e.g. a probe/availability check rather than assuming it works).
- **Failure handling:** timeouts, rate limits, and unreachable backends must surface as explicit, retryable errors. A game must never silently record a failed LLM call as a real exchange. Automated tests that don't need a real model call must use a mock backend.
- Exact stack, client, and model verification is the Architect's job (real command run, real output, per the Verified-field discipline) — the April sketch's model names are untested hypotheses, not facts to assume.

## 8. Inputs and outputs

**Inputs (configuration):**
- Scenario template (name, description, crime window, card sets, crime declaration, cover story).
- Cast configuration (role → household identity mapping).
- Backend configuration (provider, model, timeouts, retries, fallback policy).
- Run configuration (number of games, rounds per game, randomness seed for reproducibility, output paths).

**Outputs:**
- Per-game structured transcript (JSON): every exchange (speaker, action type, content, turn number), formal accusations (accuser, target, reason, turn), votes, and outcome.
- Per-game result record: traitor identity, most-accused player, traitor_caught boolean, vote tally, exchange count.
- Aggregate metrics report across the batch: catch rate, average exchanges before resolution, formal-accusation usage.
- Transcripts and results persisted to disk (structured files), not just printed.

## 9. Metrics and success criteria

**Metrics:**
1. **Traitor catch rate** — fraction of games where the Traitor is the plurality of valid final votes. Target band 40–65% is a hypothesis to calibrate from, not a pass/fail gate.
2. **Average number of exchanges before resolution.**
3. **Formal accusation usage** — whether the mechanic was used and how often.

**Success criteria:**
- The harness runs a real baseline batch (≥10 games) end-to-end via the real entry point, and the metrics output is genuine: a real, varying catch rate across real transcripts — not a stub or hardcoded result.
- Discussion quality is reviewable from transcripts (qualitative): inferences beyond own cards, contradictions raised, genuine uncertainty, L1/L2 moments — the green flags from the GM Pack.

## 10. Edge cases and failure cases (must handle)

1. **LLM backend unavailable or degraded** — Claude API down/rate-limited, or the Ollama desktop machine offline. The harness must retry with backoff up to a configured budget, then fail the game loudly with a clear error. A failed call must never be recorded as an exchange. A mock backend must exist for deterministic tests.
2. **Malformed or non-compliant agent output** — e.g. a question with no named target, a vote naming no player or multiple players, role-revealing text, out-of-character chatter, or a player fabricating observations not in their cards. The harness must validate action output, re-prompt once for format violations or log the turn as non-compliant, and continue — never crash the run. Fabricated cards must not be injected into the record as facts (a false *claim* in the transcript is legitimate gameplay and must be preserved as such).
3. **Vote tally ambiguity** — "no accusation" votes, multi-name votes, and ties. Only explicit single-name valid votes count toward a target; "no accusation" is a valid non-target vote; the Traitor is caught iff the Traitor receives strictly more valid votes than any other player; ties and no-accusation outcomes must be reported explicitly in the result record.
4. **Private-information leakage** — no player's prompt may ever contain another player's private cards, the Traitor's sealed crime/cover story, or the Detective hint. A leak corrupts the experiment itself, so prompt isolation per player is a hard requirement (assertable in tests).

## 11. Out of scope (explicitly excluded)

- Any mobile app, UI, or frontend.
- Mode A (live weekend session) or any combined full-week simulation.
- The human-run Family Prototype playtest (a separate real-world session, not software).
- Season structure, elections, Friday live events, monetisation, anti-cheat, household/IP grouping.
- Experiments 2–5 from `AI_Simulation_Design.md` (deferred follow-up).
