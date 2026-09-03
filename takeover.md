# MidTerraSim — Session Takeover

Handoff document for continuing work in a new chat. Last updated 2026-09-03
(rabbits, per-season reproduction, grass as a soft under-layer, player
fall-through fix — see [Gotchas](#gotchas) before touching player height —
world-file persistence of fauna + the simulation clock, a data-driven
`feed_radius`, and burrows/the `home` need).

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
| `main.py` | Ursina UI client. Polls `/state` on a background thread, renders terrain/vegetation/structures/creatures/drops from the snapshot, local player controls + HUD. No simulation logic. |

### Data files

| File | Role |
|------|------|
| `config.json` | Timing, seasons, `drop_lifetime` — hot-reloaded every frame |
| `entities.json` | Items, vegetation, structures, creatures — loaded at startup |
| `chunks/chunk_0_0.wrld` | Saved world, format `version: 2` (gitignored) |

### World file persistence

`Chunk` (in `chunk.py`) owns the `.wrld` format and saves terrain,
`vegetation_ages`, **fauna, structures, and the simulation clock**:
`creatures` is `{name: [instance, ...]}` keyed by `entities.json` creature
name, `structures` is a flat list keyed by `type`, each with its own id
counter, plus a `time` section (`cycle`/`season`/`day`).

- Server side: `_load_or_seed_creatures()` + `_restore_structures()` +
  `_restore_clock()` on startup; `_store_creatures_in_chunk()` +
  `_store_structures_in_chunk()` + `_store_clock_in_chunk()` on every `save()`.
- A saved structure of a type that's gone from `entities.json` is dropped on
  load, and `_evict_dangling_homes()` then clears any `home` pointing at it,
  so a creature's `home` always resolves to a live structure.
- A saved season is validated against `config.json`; an unknown name warns and
  falls back to `DEFAULT_SEASON`. The four default seasons are always merged
  into the config, so that fallback can't itself `KeyError`.
- `_restore_clock()` also seeds `_prev_is_day` from the *current* phase instead
  of assuming day. Assuming day meant a server started at night swallowed the
  next dawn (`is_day == _prev_is_day`, so the edge never fired) and undercounted
  `current_day` by one.
- `speed_multiplier`/`_time_offset` are initialized early in `World.__init__`
  because `_restore_clock()` reads `_effective_time()`.
- Matching is **by name**, so reordering `entities.json` is safe and a species
  the file has never seen (as the rabbit was) is seeded fresh rather than
  needing `generate_chunk.py`.
- `{}` (a version-1 file) means "no fauna data, seed it"; `{"rat": []}` means
  "the rats died, leave it empty" — don't collapse those two cases.
- `Chunk.normalize_creature()` coerces field types on load and drops entries
  without a usable `id`/`x`/`z`, so a hand-edited file can't feed strings into
  the simulation.
- Not persisted: item drops (their `age` is measured against simulation time),
  revision counters, the day/night phase (wall-clock derived), `speed_multiplier`.

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

**Creatures** — rat + rabbit with `needs: ["feed", "sleep", "home"]`. Rat
`diet: ["food"]` (item drops + flower attacks). Rabbit
`diet: ["grass", "bush"]` (eat grass cover, else browse bushes). Both declare
`feed_radius` (rat 5, rabbit 6), which caps the food search on either path,
and `home_gain: 0.5`, the per-day want for shelter while homeless.

**Structures** — burrow, the first one. Not a block: it lives on a tile
next to whatever vegetation is there, rendered as a `"surface"` quad above
the ground and grass. `dwellers: ["rat", "rabbit"]` (matched by creature name
*or* tag) decides who may live in it; `initial_age: 2` and
`break_chance: 0.2` drive seasonal weathering; `contains` is a persisted but
so-far-unused larder for dwellers stashing food.

Texture is `textures/burrow_grass_64.png` — see [Texture Assets](#texture-assets).

Block IDs: `AIR=0`, `GRASS=1` (bare soil), `FLOWER=2`, `BUSH=3`, `TREE=4`, `GRASS_PATCH=5`

### World drops

- Floating billboard quads with 16×16 textures (`textures/*_16.png`)
- Spawned on vegetation/creature death via `_drop_from()` (stage-aware)
- Expire after `drop_lifetime` seconds; no auto-pickup
- Eating handled only by creature feed AI

### Creature needs / feed + sleep + home AI

Rat and rabbit both define `needs: ["feed", "sleep", "home"]`. Each movement
tick per fauna instance:

1. `_compute_creature_needs()` — `feed` = `initial_hunger - hunger`; `sleep`
   = `0` if already `asleep`, else the running `sleep` accumulator; `home`
   = `0` if it has a home, else the running `home_need` accumulator
2. `_pick_highest_need()` — highest value task wins, or none if all ≤ 0
3. `_creature_move()` — execute the winning task (`sleep`/`feed`/`home`) or
   random walk

Needs compete purely by value, which sets the de-facto priority: `feed`
grows 1/day against `home`'s 0.5/day, so a hungry creature always eats
before it digs.

**Feed priority:**
1. Eat food on same tile (`_resolve_diet` → items with matching tags/names)
2. Attack flower on same tile
3. Step toward nearest food drop within `feed_radius`
4. Step toward nearest dead flower, then live flower — same radius
5. Random move

Diet `"food"` resolves to seed, berry, meat via item tags.

**Home (`_act_home`):** claims the tile the creature is standing on — adopt
the structure already there if `dwellers` allows it, else build one (never
two on a tile). Sets `home` to that structure's id and zeroes the
accumulator. A collapsing structure clears its dwellers' `home`, so they
start wanting one again.

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
| Creature AI | `_compute_creature_needs()`, `_act_feed()`, `_act_feed_plants()`, `_feed_radius()`, `_act_home()`, `_creature_move()`, `_eat_food_at_block()` |
| Structures | `_resolve_home_structure()`, `_can_dwell()`, `_structure_at()`, `_build_structure()`, `_remove_structure()`, `_break_structures()`, `_settle_home()` |
| Lifecycle | `_on_day_start()`, `_on_season_start()`, `_spawn_creature_at()`, `_remove_creature()` |
| Fauna persistence | `_load_or_seed_creatures()`, `_restore_creature_type()`, `_seed_creature_type()`, `_store_creatures_in_chunk()`, `save()` |
| Clock persistence | `_restore_clock()`, `_store_clock_in_chunk()`, `DEFAULT_SEASON`, `DAY_FRACTION` |
| API | `snapshot()`, `make_handler()` (GET `/health`, `/state`; POST `/save`) |

`main.py` (rendering only):

| Area | Functions |
|------|-----------|
| Networking | `ServerClient` (background polling thread) |
| World build | `_build_world()`, `_reset_world_state()` (on server-restart detection) |
| Sync from snapshot | `_apply_snapshot()`, `_rebuild_vegetation()`, `_rebuild_structures()`, `_sync_creatures()`, `_sync_drops()` |
| Surface layers | `build_surface_mesh(entries, sy, lift)` — `GRASS_LIFT` (0.01) then `STRUCTURE_LIFT` (0.02) stack above the terrain top so a burrow covers the grass it's dug into |
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
- Structures are flat surface meshes too, one layer above grass — burrow uses opaque 64×64 `textures/burrow_grass_64.png`, with its grass background baked in (so it keeps summer grass on fall/winter ground — revisit with an alpha surround or per-season variants if that reads badly)
- A structure whose texture won't load falls back to a flat brown quad (`main.py` checks `load_texture` for `None`), so missing art degrades instead of breaking. Ursina caches the miss per process: after adding a texture file, **restart the client** or it keeps drawing the fallback
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
16. Grass rendering switched from cross-billboard to surface decal: `stage.render: "surface"` + `textures/grass.png`, `build_surface_mesh` (then named `build_surface_veg_mesh`) in `main.py`; removed `grass_xcross_64.png`
17. Client eye height pinned to `PLAYER_EYE_HEIGHT = 2.0` (camera_pivot re-asserted on world build); walkable floor top at `floor_top(SY) = SY + 0.6`
18. Drops raised to `_drop_center_y()` so the whole billboard clears the ground and grass decal (previously half-buried at `SY + 0.52`)
19. Vegetation stage heights fixed to flower 1 / bush 2 / tree 4 — the `max_age: 99` stages (used by *fresh* plants, since age counts down) were still set to height 1
20. Fauna now reproduce on **every** season change, not just summer; winter still ages first so only survivors breed
21. Added the **rabbit**: herbivore with `diet: ["grass", "bush"]` — eats grass cover off its tile, else seeks grass within `feed_radius`, else browses bushes (age −`attack`); drops 2–3 meat
22. Runtime grass no longer blocks other flora — `ground_cover`-tagged vegetation is a soft under-layer, so flower/bush/tree can claim a grassy tile
23. Fixed endless player fall-through (thick floor collider + on-floor spawn + recovery guard) — see [Gotchas](#gotchas)
24. Fauna is now persisted in the world file (`.wrld` format `version: 2`) — creatures were previously reseeded on every server start while vegetation survived
25. The simulation clock (season/cycle/day) is persisted too, so a restart no longer snaps the world back to spring day 0
26. `feed_radius` is now data-driven for rats too (was hardcoded 5 in `_find_nearest_food_drop`/`_find_nearest_flower`); both feed paths read it through `_feed_radius()`
27. Added `structures` as a third entity category, starting with the **burrow**: rats/rabbits gain a `home` need, dig or adopt one where they stand, and get evicted when a season collapses it
28. Added [`roadmap.md`](./roadmap.md) backlog; linked from README

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

- Fill in the burrow `contains` larder — dwellers hauling food home to
  preserve it (the field already persists; nothing writes to it)
- Sleeping/breeding inside a burrow rather than wherever the creature stands
- Extend `needs` beyond `feed`/`sleep`/`home` (thirst, etc.)
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
`feed_radius` caps the search distance on both paths via `_feed_radius(cdef)`
(fallback `DEFAULT_FEED_RADIUS = 5`).

```json
{
  "name": "rat",
  "needs": ["feed", "sleep", "home"],
  "diet": ["food"],
  "feed_radius": 5,
  "initial_hunger": 3,
  "attack": 1,
  "sleep_gain": 0.5,
  "home_gain": 0.5,
  "avoids_block_tag": "tree",
  "reproduce_count": [1, 6]
}
```

```json
{
  "name": "rabbit",
  "needs": ["feed", "sleep", "home"],
  "diet": ["grass", "bush"],
  "feed_radius": 6,
  "initial_hunger": 5,
  "initial_age": 3,
  "attack": 1,
  "sleep_gain": 0.5,
  "home_gain": 0.5,
  "avoids_block_tag": "tree",
  "reproduce_count": [2, 3],
  "contains": [{ "item": "meat", "count": [2, 3] }]
}
```

## Quick Reference: Structure Configs

Per-instance state lives in `world_structures` (`id`/`type`/`x`/`z`/`age`/
`contains`); the definition below only seeds it. A creature's `home` holds
the instance `id`, or `None` while homeless.

```json
{
  "name": "burrow",
  "tags": ["structure", "shelter"],
  "texture": "textures/burrow_grass_64.png",
  "render": "surface",
  "initial_age": 2,
  "break_chance": 0.2,
  "dwellers": ["rat", "rabbit"],
  "contains": []
}
```
