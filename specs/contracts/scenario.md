---
id: traitors-mobile-scenario
type: interface-contract
project: traitors-mobile
parents: [SWA-146]
status: draft
version: 1
paperclip_issue: SWA-146
owner_role: Architect
created: 2026-09-01
updated: 2026-09-01
---

# Module: Scenario (traitors-mobile-scenario)

## Purpose
Defines, loads, validates and initialises the concrete game scenario: the public scenario text, the fixed 5-player cast (1 Traitor, 1 Detective, 3 Loyalists), per-player role cards (goal, private observations, Traitor's sealed crime declaration + cover story, Detective's directional hint), and the crime window. Satisfies spec §3–§5: "Scenario generation — produces at least one concrete scenario (baseline: the stolen prize tin) with a fixed cast and per-player private role material." The baseline scenario content is baked into this contract (below) so the Engineer builds data, not invention.

## Depends on
- None (pure data + stdlib). First module built.

## Constraints (non-goals)
- No LLM calls, no network, no writes to disk. Scenario data loading is read-only.
- No game logic (no turn driving, no rules) — that is the Orchestrator's module.
- Card content must be *equivalent* to the GM Pack facts (times, places, sightings), not prose copied from the source docs (spec §4).
- No fabrication guard: scenario only grants each player their own private material; it must never expose another player's material to a player (leakage prevention is exercised in tests here and enforced in the Player module).

## External dependencies
- Python 3.11 stdlib only (`json`, `dataclasses`, `random` for seedable assignment, `pathlib`).
- **Verified:** `python3 --version` → 3.11.15; stdlib modules confirmed present by construction.

## Baseline scenario data (the only scenario required — "stolen prize tin", from spec §4–5)

Public scenario text: "Meadowbrook Community Summer Fair, Saturday evening. The prize fund — £340 in a locked tin — was confirmed present and locked at 7:30pm by two committee members. At 9:15pm, event organiser Janet Marsh discovered it missing from the community centre office. The office was left unlocked during the event, as normal when the building is staffed. Everyone in the group was present during the relevant window (7:30pm–9:15pm). At least one person in the group stole the tin; it may have been done alone."
Crime window: `8:40pm–8:55pm`.

Cast (role → household), the baseline from spec §3:
| Role | Household |
|---|---|
| traitor | The Abbotts |
| detective | The Murphys |
| loyalist_a | The Chens |
| loyalist_b | The Patels |
| loyalist_c | The Okayes |

Role cards (private material per player):

- **traitor (The Abbotts):** goal "Avoid being formally accused; redirect suspicion; may lie; may share or withhold cards selectively (never reveal the crime declaration)." Observations: (1) "Signed the visitor book on arrival at 7:10pm — earlier than most people." (2) "Noticed the office door was unlocked at ~8:40pm while walking past on the way to the toilets." (3) "When re-entering the bar at 8:57pm, noticed The Patels (Loyalist B) looking in their direction." Sealed crime declaration: "Slipped away from the bar at 8:45pm, told no one, went to the office, found it unlocked, took the tin, hid it in their bag, back at the bar by 8:57pm (just before the 9:00pm toast)." Cover story: "At the bar the entire evening from 8:20pm; bought a round of drinks at ~8:30pm; never left the bar area."
- **detective (The Murphys):** goal "Help identify the thief; share observations strategically; corroborate others; watch for inconsistencies between claims and known facts." Observations: (1) "At the buffet table 8:30pm–9:05pm with a clear sightline to the corridor leading to the office." (2) "Between 8:40pm and 9:00pm, two separate people left the main hall toward the corridor; couldn't clearly identify the second; the first was The Chens (Loyalist A)." (3) "Spoke to the caretaker at 9:10pm; he mentioned someone had asked him about the heating earlier — odd, because the heating was fine all night." Directional hint (private): "Believes the theft occurred between 8:40pm and 8:55pm."
- **loyalist_a (The Chens):** goal "Help the group; share observations honestly; be sceptical of claims that don't add up." Observations: (1) "Talking to the event organiser near the stage 8:15pm–8:50pm." (2) "At ~8:40pm noticed the office corridor door ajar; assumed the caretaker." (3) "Walked through the corridor at 8:50pm — office door closed by then." (4) "Passed someone in the corridor at ~8:55pm heading toward the main hall; no clear look."
- **loyalist_b (The Patels):** goal "Help the group; pay attention to what doesn't add up." Observations: (1) "At the bar from 8:20pm until the 9:00pm toast." (2) "Fairly confident {traitor} were at the bar earlier, but absent from ~8:45pm until just before the toast." (3) "Saw the caretaker near the office corridor at ~8:35pm, then not again until after the toast." (4) "When {traitor} returned just before the toast, they seemed slightly flustered (could be misremembering)." — `{traitor}` is a **placeholder that must be substituted with the assigned traitor household name** during initialisation (spec §4 card-parameterisation requirement).
- **loyalist_c (The Okayes):** goal "Help the group; ask good questions and apply pressure; has less information than others." Observations: (1) "Went to the cloakroom (far end of the office corridor) at 8:58pm." (2) "Passing the office at 8:58pm, door closed, nothing unusual." (3) "Came back through the corridor at 9:03pm — office door now open, light on; assumed someone working late." (4) "No strong observations about who was where."

## Interface

### `load_scenario(path: str | Path) -> ScenarioDefinition`
- Behavior: reads a JSON file and validates it as a `ScenarioDefinition` (must contain: `id`, `name`, `description` (public text), `crime_window`, `cast` (role→household mapping), `role_cards` (per-role cards with `goal` and `observations`; traitor also `crime_declaration` + `cover_story`; detective also `detective_hint`)). Any card observation may contain `{placeholder}` tokens referencing role names (e.g. `{traitor}`).
- Raises: `FileNotFoundError` when the path does not exist; `json.JSONDecodeError` when the file is not valid JSON; `ScenarioValidationError` (message lists all problems) when required keys are missing or of the wrong type.
- Side effects: none.

### `build_scenario(defn: ScenarioDefinition, seed: int | None = None) -> Scenario`
- Behavior: validates the definition, assigns each role to its configured household, substitutes every `{role}` placeholder in every card's observation/goal/declaration/hint with the assigned household name (case preserved), and returns an initialised `Scenario` containing: `scenario_id`, `description` (public), `crime_window`, `players` (list of `PlayerIdentity` with `player_id` = household name, `role`, `household`), `players_by_role` (dict role → player_id), and per-player `RoleCard` objects.
- Raises: `ScenarioError` when a card references an unknown `{placeholder}` (not a role in the cast); `ScenarioValidationError` when the cast does not contain exactly the 5 baseline roles (traitor, detective, loyalist_a, loyalist_b, loyalist_c).
- Side effects: none. Deterministic given the same definition + seed (seed may control nothing today but is reserved for future cast-shuffling; keep the signature).

### `validate_scenario(scenario: Scenario) -> list[str]`
- Behavior: returns a list of human-readable problem strings; an empty list means the scenario is valid. Checks: all 5 roles present; every card has a non-empty goal and ≥1 observation; traitor card has `crime_declaration` and `cover_story`; detective card has `detective_hint`; **no `{placeholder}` token remains un-substituted in any card**; no player_id appears twice.
- Raises: never (returns problems instead).
- Side effects: none.

### `default_scenario() -> ScenarioDefinition`
- Behavior: returns the built-in "stolen prize tin" `ScenarioDefinition` (content above).
- Raises: never.
- Side effects: none.

## Reuse check
Searched existing `specs/` contracts in this repo and other repos under `~/sdd-projects/` for: `scenario`, `role card`, `social deduction`, `simulation`. Found: none — this is the first project with interface contracts. The scenario-definition schema and `build_scenario` placeholder-substitution logic are **moderate reuse candidates** (Experiments 2–5 will need new scenarios against the same schema), so the data model must stay generic (role-based, not household-hardcoded).

## QA acceptance highlights (behavioral)
- `load_scenario` on the built-in JSON round-trips and `build_scenario` output passes `validate_scenario` (empty problem list).
- Loyalist B's card contains the substituted household name of the traitor, and NO `{...}` tokens remain anywhere.
- No player's `RoleCard` contains another player's private observations/crime declaration/hint (leakage check at data level).
- A deliberately broken definition (missing crime window; unknown placeholder `{bogus}`) raises the documented error types.
