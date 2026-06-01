# Demo Trace: Hostile Interrupts Pickup

This walkthrough is generated from the deterministic simulator scenario `hostile_interrupts_pickup`. The simulator uses the same action catalog, procedure state, auto-continuation, and execution code as the live NetHack runner, but the map and hostile appearance are scripted so the behavior is reproducible.

This is a deterministic policy-harness trace. The harness selects from the same bounded action catalog a model would receive, while the map and hostile appearance are scripted for reproducibility. The trace focuses on affordance generation, procedure continuation, interruption, and tactical handoff.

## Situation

The player starts beside a visible item:

```text
@$...
.....
.....
```

There is no hostile pressure in the initial scene. The action catalog exposes one useful high-level action:

```text
pick:item:gold -> move(east)
```

## Step 1: Policy Chooses The Item Intent

The model-facing policy makes a bounded choice from the catalog:

```json
{
  "decision": "switch",
  "chosen_action_id": "pick:item:gold",
  "reason": "scripted simulator policy"
}
```

The runtime executes the first low-level step:

```text
move(east)
```

After that move, the player is standing on the item. The pickup procedure is still active and would normally continue with:

```text
pickup()
```

## Step 2: Hostile Interrupt

The scenario then reveals a goblin adjacent to the player:

```text
A goblin steps into view.
.$o..
.@...
.....
```

The next scene includes the event:

```text
goblin is adjacent and threatening you
```

The action catalog now contains:

```text
flee:monster:goblin
fight:monster:goblin
pick:item:gold
```

The interrupted pickup action is still visible, but the runtime does not auto-continue it because nearby hostile pressure invalidates the procedure. The policy receives the same constrained tactical catalog a model would receive at this handoff and selects:

```json
{
  "decision": "switch",
  "chosen_action_id": "flee:monster:goblin",
  "reason": "scripted simulator policy"
}
```

The runtime executes:

```text
move(southwest)
```

## What This Trace Shows

This trace exercises the intended runtime boundary:

- The decision layer chooses semantic intent.
- Runtime owns the mechanical chain for item pickup.
- Observation changes can interrupt an active procedure.
- Unsafe continuation is blocked by code before the decision layer can blindly continue.
- The handoff exposes a constrained tactical catalog and switches to flee.

The decision layer operates inside a constrained interface. Runtime code manages execution, interruption, and safety in a changing environment.
