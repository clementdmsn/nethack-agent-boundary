# Demo Trace: Corridor Follow To Intersection

This walkthrough is based on a live auto-mode trace saved as `docs/demo/traces/corridor_follow.trace.json`.

At runtime, `logs/last_execution_trace.md` is overwritten at launch and then records the current run chronologically. Use `/save-trace <name>` when a trace is interesting enough to keep as a curated demo artifact.

The trace shows one model/runtime handoff. The model receives a structured scene and a catalog of valid affordances, chooses one grounded high-level action, and the runtime follows the corridor until it reaches an intersection.

## Situation

The player is idle in a corridor with no visible hostile pressure.

Relevant scene facts:

- Player: `priest called agent_d74c5656`
- Area type: corridor
- Adjacent corridors: north, west, east, northeast
- Adjacent doors: none
- Recent low-level actions: five westward moves
- Visible non-hostile facts:
  - tame kitten called Kitty, 5 steps east
  - a scroll labeled `LEP GEX VEN ZEA`, 7 steps east
  - a statue of a lichen, 9 steps east

The current scene summary in the trace:

```text
Visible area. Visible facts: tame kitten called Kitty is 5 steps away via east, east, east, east, east; a statue of a lichen is 9 steps away via east, east, east, east, east, east, east, east, east; a scroll labeled LEP GEX VEN ZEA is 7 steps away via east, east, east, east, east, east, east.
```

## Available Actions

The action catalog offered three grounded corridor actions:

- `explore_corridor:west`
  - label: Follow corridor west
  - next action: `follow_corridor(west)`
  - priority: nearest exploration
  - continues recent direction: yes
- `explore_corridor:north`
  - label: Follow corridor north
  - next action: `follow_corridor(north)`
- `explore_corridor:northeast`
  - label: Follow corridor northeast
  - next action: `follow_corridor(northeast)`

The action ids are generated from the observed scene. The model selects one of those affordances; the runtime translates it into the low-level movement sequence.

## Model Decision

The model selected the preferred corridor action:

```json
{
  "decision": "switch",
  "chosen_action_id": "explore_corridor:west",
  "reason": "The player is currently in a corridor with adjacent west direction, and following the west corridor aligns with nearest exploration priority while ensuring safe movement. No hostile threats are present, and this action directly supports the goal of methodically exploring new areas."
}
```

This was a good decision because:

- there was no active procedure to continue
- no hostile was visible
- west was marked as nearest exploration
- west continued recent movement direction
- corridor traversal is a runtime-managed procedure

In current runs, the prompt asks the model for much shorter reasons. The behavior under review is the same: the model picks the grounded intent and the runtime emits the keystrokes.

## Runtime Execution

The selected high-level action was:

```text
explore_corridor:west
```

The runtime activated the corridor-follow procedure and executed ordinary movement keys internally. It moved 3 steps total and stopped with:

```json
{
  "status": "corridor_follow_intersection",
  "scene_changed": true,
  "steps": 3
}
```

The final low-level action recorded in the trace was:

```text
move(southwest)
```

That is expected. The model chose the initial corridor intent, then the deterministic corridor procedure followed the visible corridor topology as it turned.

## Resulting State

After execution, the player was still in a corridor, now at an intersection-like decision point:

- Adjacent corridors: south, east, northeast, southwest
- Adjacent doors: none
- Newly visible or repositioned facts:
  - branch staircase up at `[2, -7]`
  - open door at `[11, -3]`
  - scroll and statue now farther away relative to the new player position

The runtime marked the procedure as blocked:

```json
{
  "action_id": "explore_corridor:west",
  "status": "blocked",
  "next_action": null,
  "low_level_goal": null
}
```

In this context, `blocked` means the internal corridor procedure intentionally stopped and the model should reassess. An intersection is a tactical choice point, so stopping there is the correct behavior.

Note: current corridor-room entrance behavior is different. When following a corridor reaches a new room, that is now treated as successful arrival: the procedure completes and the next model turn receives a directive event to analyze the room instead of immediately backtracking.

## What This Trace Shows

This trace exercises several runtime properties:

- The model receives structured state and a constrained action catalog.
- The model chooses a semantic action.
- Code handles repetitive mechanical execution.
- The runtime stops at a meaningful decision boundary.
- The trace records enough information to audit every step:
  - model input
  - available actions
  - raw response
  - parsed decision
  - selected action
  - executed low-level action
  - outcome
  - scene after execution

The model decides that the corridor is worth following. The runtime handles the movement details and stops at the next tactical branch.
