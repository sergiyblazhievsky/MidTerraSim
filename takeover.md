# MidTerraSim — Session Takeover

Handoff document for continuing work in a new chat. Last updated after the
client/server split (headless `server.py` + Ursina `main.py` UI client).

---

## Project Summary

**MidTerraSim** is a client/server 3D ecosystem simulation (Python + [Ursina Engine](https://www.ursinaengine.org/)). A headless `server.py` process owns the authoritative world and runs every simulation timer (vegetation, seasons, day/night, creature AI, drops, persistence) and exposes it over a small stdlib HTTP/JSON API. A separate `main.py` Ursina UI client polls that API, renders the world, and provides first-person controls — it holds no authoritative state and can be started/stopped/restarted independently of the server. See [`README.md`](./README.md) for the full architecture and configuration reference, and [`SERVER_CLIENT_API.md`](./SERVER_CLIENT_API.md) for the complete HTTP/JSON API contract (schema, headers/status codes, thread-safety, polling/reconnect guidance, and client examples) if you're building your own client against the server.

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
| `server.py` | Headless, no Ursina import. Authoritative `Chunk`/ecology state, all timers, HTTP API (`/health`, `/state`, `/save`), persistence. |
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

**Vegetation** — flower, bush, tree with stages, spawn rules, per-stage loot

**Creatures** — rat with `needs: ["feed", "sleep"]`, `diet: ["food"]`

Block IDs: `AIR=0`, `GRASS=1`, `FLOWER=2`, `BUSH=3`, `TREE=4`

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

Priority order: flower → bush → tree (first win per tile).

Global gate: bare grass + fertility roll + not already changing.

| Plant | Chance | Key constraints |
|-------|--------|-----------------|
| Flower | 9% | max 1 neighbor flower (r=1); **no tree/bush blockers** |
| Bush | 3% | no tree/bush within r=1 |
| Tree | 1% | no tree within r=2, no bush within r=1 |

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

## Texture Assets

- **16×16** — ground tiles, drop icons (`*_16.png`)
- **64×64 cross** — vegetation billboards (`*_xcross_64.png`)
- `generate_chunk.py` also generates legacy `textures/seed.png`; runtime drops use `*_16.png`

---

## Session Work Log (2026-09-03)

1. Split the monolithic `main.py` into headless `server.py` (authoritative simulation + HTTP API) and a rendering-only `main.py` UI client
2. Added stdlib-only HTTP/JSON API: `GET /health`, `GET /state` (full renderable snapshot with monotonic `revision`/`vegetation_revision`), `POST /save`
3. Client polls `/state` on a background thread; disconnect/reconnect handled without restarting either process; detects a server-restart (revision going backwards) and does a full entity rebuild
4. Config gained `server` (host/port/tick_rate/save_interval) and `client` (host/port/poll_interval/request_timeout) sections
5. `chunk.py` gained `overrides_at_y()` read-only helper used by the server's snapshot builder
6. Sleep-need numeric behavior preserved exactly (no threshold, +0.5/night movement cycle, resets at day start)
7. Added standalone [`SERVER_CLIENT_API.md`](./SERVER_CLIENT_API.md) — the full HTTP/JSON API contract for third-party clients (exact schema/types/nullability, headers/status codes verified against a running server, thread-safety rules, `entities.json` texture/stage resolution, coordinate/`surface_y` assumptions, polling/reconnect/sync recommendations, curl/PowerShell/Python examples, compatibility guidance, and limitations); trimmed README's HTTP API section to a concise overview that links to it; fixed stale `takeover.md` references to rat `needs` (was documented as `["feed"]` only, actually `["feed", "sleep"]`)

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
- Generalize feed attack beyond flowers (bushes? trees?)
- Align `generate_chunk.py` initial spawn order with runtime (flower first vs last)
- `map_viewer.py` — show bushes/trees, not just flowers
- Add `log`/`stick` item definitions with tags if needed for crafting later
- If terrain height ever becomes non-flat, extend `/state` with a heightmap (client currently assumes a constant `surface_y`)

---

## Quick Reference: Rat Config

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
