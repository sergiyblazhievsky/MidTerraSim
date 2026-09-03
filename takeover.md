# MidTerraSim — Session Takeover

Handoff document for continuing work in a new chat. Last updated 2026-09-03
(rabbits, per-season reproduction, grass as a soft under-layer, player
fall-through fix — see [Gotchas](#gotchas) before touching player height).

---

## Project Summary

**MidTerraSim** is a client/server 3D ecosystem simulation (Python + [Ursina Engine](https://www.ursinaengine.org/)). A headless `server.py` process owns the authoritative world and runs every simulation timer (vegetation, seasons, day/night, creature AI, drops, persistence) and exposes it over a small stdlib HTTP/JSON API. A separate `main.py` Ursina UI client polls that API, renders the world, and provides first-person controls — it holds no authoritative state and can be started/stopped/restarted independently of the server. See [`README.md`](./README.md) for the full architecture and configuration reference, [`SERVER_CLIENT_API.md`](./SERVER_CLIENT_API.md) for the complete HTTP/JSON API contract (schema, headers/status codes, thread-safety, polling/reconnect guidance, and client examples) if you're building your own client against the server, and [`roadmap.md`](./roadmap.md) for planned-but-not-yet-implemented work.

```
python server.py         # start the simulation (run this first, own console)
python main.py            # start/stop/restart the UI client any time
python generate_chunk.py  # reset world + regenerate base textures
python map_viewer.py      # top-down .wrld inspector (Tkinter)
```

Requirements: `pip install ursina pillow`

---

## Current Architecture

### Processes

| File | Role |
|------|------|
| `server.py` | Headless, no Ursina import. Authoritative `Chunk`/ecology state, all timers, HTTP API (`/health`, `/state`, `/admin`, `/save`, `/admin/speed_multiplier`), persistence. |
| `main.py` | Ursina UI client. Polls `/state` on a background thread, renders terrain/vegetation/creatures/drops from the snapshot, local player controls + HUD. No simulation logic. |

### Data files

| File | Role |
|------|------|
| `config.json` | Timing, seasons, `drop_lifetime` — hot-reloaded every frame |
| `entities.json` | Items, vegetation, creatures — loaded at startup |
| `chunks/chunk_0_0.wrld` | Saved world (gitignored) |

### API documentation

[`SERVER_CLIENT_API.md`](./SERVER_CLIENT_API.md) is the standalone contract for `server.py`'s
HTTP/JSON API — exact `/health`, `/state`, `/save` request/response
behavior, full `/state` schema (field-by-field types and nullability),
thread-safety/authoritative-state rules, the `entities.json`
texture/stage-resolution algorithm a client must reimplement locally, and
curl/PowerShell/Python client examples. It is the reference to update
whenever the wire schema, endpoints, or headers change — keep it in sync
with `World.snapshot()` and `make_handler()` in `server.py`.

### `entities.json` structure

**Items** — seed, berry, meat tagged `raw` + `food`

**Vegetation** — flower, bush, tree, grass with stages, spawn rules, per-stage loot.
Grass uses `stage.render: "surface"` (flat tile texture over soil); other flora
default to `"cross"` (vertical billboard).

**Creatures** — rat + rabbit with `needs: ["feed", "sleep"]`. Rat
`diet: ["food"]` (item drops + flower attacks). Rabbit
`diet: ["grass", "bush"]` (eat grass cover, else browse bushes within
`feed_radius`).

Block IDs: `AIR=0`, `GRASS=1` (bare soil), `FLOWER=2`, `BUSH=3`, `TREE=4`, `GRASS_PATCH=5`

### World drops

- Floating billboard quads with 16×16 textures (`textures/*_16.png`)
- Spawned on vegetation/creature death via `_drop_from()` (stage-aware)
- Expire after `drop_lifetime` seconds; no auto-pickup
- Eating handled only by creature feed AI

### Creature needs / feed + sleep AI

Rats currently define `needs: ["feed", "sleep"]`. Each movement tick per
fauna instance:

1. `_compute_creature_needs()` — `feed` = `initial_hunger - hunger`; `sleep`
   = `0` if already `asleep`, else the running `sleep` accumulator
2. `_pick_highest_need()` — highest value task wins, or none if all ≤ 0
3. `_creature_move()` — execute the winning task (`sleep`/`feed`) or random walk

**Feed priority:**
1. Eat food on same tile (`_resolve_diet` → items with matching tags/names)
2. Attack flower on same tile
3. Step toward nearest food drop (5 block radius)
4. Step toward nearest dead flower, then live flower
5. Random move

Diet `"food"` resolves to seed, berry, meat via item tags.

**Sleep:** no threshold — it's a plain value that competes with `feed` for
priority. Each night movement cycle a creature is awake, `sleep` increases
by `sleep_gain` (default `0.5`); if `sleep` wins the priority pick, the
creature stops moving and is marked `asleep` (rendered with `sleep_texture`
by the client). `sleep` resets to `0` and `asleep` clears at the start of
each day (`_on_day_start` → `_wake_creature`). See README's
[Simulation Overview](./README.md#simulation-overview-server-authoritative)
for the full day/night and lifecycle rules.

### Flora spawn (runtime `_sim_step`)

Priority order: flower → bush → tree → grass (first win per tile).

Global gate: bare ground + fertility roll + not already changing.

| Plant | Chance | Key constraints |
|-------|--------|-----------------|
| Flower | 9% | max 1 neighbor flower (r=1); **no tree/bush blockers** |
| Bush | 3% | no tree/bush within r=1 |
| Tree | 1% | no tree within r=2, no bush within r=1 |
| Grass | 15% | `spawn.active_seasons: ["spring", "summer"]` only; no proximity blockers |

`spawn.active_seasons` (optional, list of season names) is a generic gate any
vegetation entry can use — checked before the chance roll, skips the entry
entirely outside those seasons. Grass is the first (and currently only) user
of it. `generate_chunk.py` separately covers every bare-soil tile with grass
unconditionally at world-gen time (100%); the runtime rule above only handles
re-growth afterward (e.g. once a tree/bush/flower dies back to bare soil).
Grass otherwise behaves like any other `flora`-tagged vegetation — it decays
via the same moisture-driven mechanic, just with no stage-based loot.

### Stage-based loot

See README for full table. Bush and tree loot is per-stage in `entities.json`.

---

## Key Code Locations

`server.py` (`World` class — authoritative):

| Area | Methods |
|------|-----------|
| Entity loading | `load_entities()`, `_veg_with_tag()`, `_items_with_tag()`, `_resolve_diet()` |
| World drops | `_spawn_drop()`, `_drop_from()`, `_update_drops()` |
| Simulation | `_sim_step()`, `_count_kind_near()`, `tick()` |
| Creature AI | `_compute_creature_needs()`, `_act_feed()`, `_creature_move()`, `_eat_food_at_block()` |
| Lifecycle | `_on_day_start()`, `_on_season_start()`, `_spawn_creature_at()`, `_remove_creature()` |
| API | `snapshot()`, `make_handler()` (GET `/health`, `/state`; POST `/save`) |

`main.py` (rendering only):

| Area | Functions |
|------|-----------|
| Networking | `ServerClient` (background polling thread) |
| World build | `_build_world()`, `_reset_world_state()` (on server-restart detection) |
| Sync from snapshot | `_apply_snapshot()`, `_rebuild_vegetation()`, `_sync_creatures()`, `_sync_drops()` |
| Visual-only timers | `_update_lighting_and_bob()` (day/night + drop bob extrapolation between polls) |

---

## Gotchas

### Player fall-through: the floor collider must stay thick

**Symptom:** you spawn and fall forever through the ground into the void, with
no error in either console. Has been hit twice now.

**Cause:** Ursina's `FirstPersonController` moves a falling player by
`air_time * time.dt * 100` per frame, so the step size scales with frame time.
The frame that runs `_build_world()` (10,000-quad ground mesh) plus the first
`_rebuild_vegetation()` (thousands of quads) takes on the order of a second, so
the next frame's `time.dt` is huge and one gravity step can travel more than a
block. That skips a thin floor collider entirely — and once the player is
underneath it, the downward raycast hits nothing, `ray.distance` is `inf`, and
they fall forever. Nothing logs, because nothing actually failed.

**Guards in `main.py` — keep all three:**

1. `FLOOR_THICKNESS = 20` — the walkable slab is thick enough that no single
   gravity step can pass through it. Only its **top** face matters visually;
   it's positioned at `floor_top(SY) - FLOOR_THICKNESS / 2`. The placeholder
   `_temp_floor` uses the same thickness.
2. `_build_world()` spawns the player with feet already **on** the floor
   (`player.position = (sx / 2, floor_top(SY), sz / 2)`, `air_time = 0`)
   instead of dropping them onto it — no free fall during the slow frame.
3. `update()` snaps the player back to `floor_top(SY)` if they ever end up more
   than a unit below it.

**Height model** — one source of truth, don't reintroduce half-unit offsets:

| Thing | Y |
|-------|---|
| Visual ground / plant bases / grass decal | `surface_top(SY)` = `SY + 0.5` |
| Walkable floor top (player feet) | `floor_top(SY)` = `SY + 0.6` |
| Camera / eyes | feet + `PLAYER_EYE_HEIGHT` (2) = `SY + 2.6` |

Debugging this needs the **real client against a live server** — the geometry
grounds fine in an isolated Ursina scene, because the bug only appears once
frame times spike from actual mesh building.

---

## Texture Assets

- **16×16** — ground tiles (`soil*.png`, `grass.png` surface cover), drop icons (`*_16.png`)
- **64×64 cross** — vegetation billboards (`*_xcross_64.png`) for flower/bush/tree
- Grass patches are **not** cross-billboards; they use opaque `textures/grass.png` as a flat surface mesh
- `generate_chunk.py` also generates legacy `textures/seed.png`; runtime drops use `*_16.png`

---

## Session Work Log (2026-09-03)

1. Split the monolithic `main.py` into headless `server.py` (authoritative simulation + HTTP API) and a rendering-only `main.py` UI client
2. Added stdlib-only HTTP/JSON API: `GET /health`, `GET /state` (full renderable snapshot with monotonic `revision`/`vegetation_revision`), `POST /save`
3. Client polls `/state` on a background thread; disconnect/reconnect handled without restarting either process; detects a server-restart (revision going backwards) and does a full entity rebuild
4. Config gained `server` (host/port/tick_rate/save_interval) and `client` (host/port/poll_interval/request_timeout) sections
5. `chunk.py` gained `overrides_at_y()` read-only helper used by the server's snapshot builder
6. Sleep-need numeric behavior preserved exactly (no threshold, +0.5/night movement cycle, resets at day start)
7. Added standalone [`SERVER_CLIENT_API.md`](./SERVER_CLIENT_API.md) — the full HTTP/JSON API contract for third-party clients
8. Added automated test suite under [`tests/`](./tests) (pytest)
9. Set full-grown tree stages to optional `"width": 2.0`; client `build_veg_mesh` honors per-stage width
10. Browser inspector at `GET /` — collapsible Vegetation/Creatures/Drops + computed `needs`; snapshot gained `vegetation[].type` and `creatures[].needs`
11. Admin `speed_multiplier` (1–100, transient) via `/admin` + inspector Admin panel
12. Split inspector status into bold Season/Cycle/Day line + secondary revision line
13. Replaced default green ground with seasonal soil textures (`soil.png` / `soil_fall.png` / `soil_winter.png`)
14. Added decorative grass vegetation (`GRASS_PATCH=5`, season-gated spawn, world-gen fills bare soil)
15. Inspector Vegetation section filters out `type === 'grass'` (display-only; still in `/state`)
16. Grass rendering switched from cross-billboard to surface decal: `stage.render: "surface"` + `textures/grass.png`, `build_surface_veg_mesh` in `main.py`; removed `grass_xcross_64.png`
17. Client eye height pinned to `PLAYER_EYE_HEIGHT = 2.0` (camera_pivot re-asserted on world build); walkable floor top at `floor_top(SY) = SY + 0.6`
18. Drops raised to `_drop_center_y()` so the whole billboard clears the ground and grass decal (previously half-buried at `SY + 0.52`)
19. Vegetation stage heights fixed to flower 1 / bush 2 / tree 4 — the `max_age: 99` stages (used by *fresh* plants, since age counts down) were still set to height 1
20. Fauna now reproduce on **every** season change, not just summer; winter still ages first so only survivors breed
21. Added the **rabbit**: herbivore with `diet: ["grass", "bush"]` — eats grass cover off its tile, else seeks grass within `feed_radius`, else browses bushes (age −`attack`); drops 2–3 meat
22. Runtime grass no longer blocks other flora — `ground_cover`-tagged vegetation is a soft under-layer, so flower/bush/tree can claim a grassy tile
23. Fixed endless player fall-through (thick floor collider + on-floor spawn + recovery guard) — see [Gotchas](#gotchas)
24. Added [`roadmap.md`](./roadmap.md) backlog; linked from README

## Session Work Log (2026-09-02)

1. Updated README for data-driven architecture
2. Drop textures mapped to `*_16.png`
3. Creature `needs` system with `feed` behavior
4. Item tags (`raw`, `food`) for seed/berry/meat
5. Stage-based bush/tree loot tables
6. Feed AI uses `food` tag (seed, berry, meat) instead of hardcoded seeds
7. Flower spawn: removed tree/bush blockers, chance 7% → 9%
8. Updated README, takeover, committed and pushed

---

## Likely Next Steps

- Extend `needs` beyond `feed`/`sleep` (thirst, shelter, etc.)
- Predators (fox/wolf) — needs a new "hunt" behavior; the plant-diet path
  (`_act_feed_plants`) is the closest existing template
- Align `generate_chunk.py` initial spawn order with runtime (flower first vs last)
- `map_viewer.py` — show bushes/trees/grass, not just flowers
- Flora variety / new fauna / generated heightmap — see roadmap
- If terrain height ever becomes non-flat, extend `/state` with a heightmap (client currently assumes a constant `surface_y`)

---

## Quick Reference: Creature Configs

`diet` drives *which* feed AI runs: item names/tags → drops + flower attacks
(`_act_feed`); vegetation names/tags → grazing/browsing (`_act_feed_plants`).

```json
{
  "name": "rat",
  "needs": ["feed", "sleep"],
  "diet": ["food"],
  "initial_hunger": 3,
  "attack": 1,
  "sleep_gain": 0.5,
  "avoids_block_tag": "tree",
  "reproduce_count": [1, 6]
}
```

```json
{
  "name": "rabbit",
  "needs": ["feed", "sleep"],
  "diet": ["grass", "bush"],
  "feed_radius": 6,
  "initial_hunger": 5,
  "initial_age": 3,
  "attack": 1,
  "sleep_gain": 0.5,
  "avoids_block_tag": "tree",
  "reproduce_count": [2, 3],
  "contains": [{ "item": "meat", "count": [2, 3] }]
}
```
