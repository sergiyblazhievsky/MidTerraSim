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

**Vegetation** — flower, bush, tree, grass with stages, spawn rules, per-stage loot

**Creatures** — rat with `needs: ["feed", "sleep"]`, `diet: ["food"]`

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
8. Added an automated test suite under [`tests/`](./tests) (pytest): 114 tests covering `chunk.py` (100%) and `server.py` (94% — only `main()`'s CLI entrypoint/tick-loop is excluded, since that's console wiring rather than testable logic). Uses `tmp_path`-isolated fixtures (`tests/conftest.py`) so tests never touch the real `config.json`/`entities.json`/`chunks/chunk_0_0.wrld`; a real `ThreadingHTTPServer` on an ephemeral port is used for API integration tests. Added `pytest.ini` and `requirements-dev.txt`; documented in README's new "Running Tests" section.
9. Set full-grown tree stages (height 4.0) to `"width": 2.0` in `entities.json` (dead/dry and mature stages); `main.py`'s `build_veg_mesh`/`_rebuild_vegetation` now read an optional per-stage `width` (defaults to `1.0` for backward compatibility) to size the cross-quad's horizontal footprint independently of height.
10. Added a browser-based debug/inspector page: `GET /` on `server.py` now serves a self-contained HTML+CSS+JS page (no external assets, no new dependency) that polls `/state` and renders a collapsible tree — Vegetation/Creatures/Drops grouped by `type`/`item`, with creatures broken out per-instance showing raw stats plus a nested `needs` group. Expand/collapse state survives the 1s poll via a document-level capturing `toggle` listener keyed by a stable `data-key` per node. To support it, `snapshot()` gained two additive fields: `vegetation[].type` (vegetation definition name, mirrors `creatures[].type`) and `creatures[].needs` (the same dict `_compute_creature_needs` uses internally, so the inspector shows *exactly* why a creature is behaving a certain way). Both fields are documented in `SERVER_CLIENT_API.md` and covered by new tests (116 total now).
11. Added an admin runtime control: `speed_multiplier` (1-100, transient, resets to 1 on restart). `GET /admin` reads it, `POST /admin/speed_multiplier` (JSON `{"value": N}`) sets it with validation. Implemented as a virtual-time offset (`World._effective_time()` = `time.time() + _time_offset`) added on top of the real wall clock, plus a `scaled_dt = dt * speed_multiplier` fed into every per-tick accumulator (`_creature_timers`, `_sim_timer`, `_save_timer`) — this scales day/night, creature movement/needs, the flora sim cycle, periodic saves, and item-drop aging *uniformly*, and is a byte-for-byte no-op at the default multiplier of `1` (offset never leaves `0`), so it required zero changes to any pre-existing test. Drop `spawn_time`/age tracking and `snapshot()`'s `now` were switched from raw `time.time()` to `_effective_time()` for consistency. Exposed in the `/` inspector page as an "Admin" panel (number input + Apply button, live-synced `speed_multiplier` display). Documented in `SERVER_CLIENT_API.md` §4b and `README.md`; 23 new tests added (139 total), 95% server.py coverage, verified stable across repeated runs and via a live browser smoke test.
12. Split the inspector page's combined status line into a dedicated, bold "Season/Cycle/Day" line plus a smaller secondary technical line (revision/updated timestamp) — the info was already there but easy to overlook mixed together.
13. Replaced the green grass ground look with soil: generated `textures/soil.png`/`soil_fall.png`/`soil_winter.png` (procedural earth/mud tones matching the existing per-season variation pattern), updated every `grass.png`/`grass_fall.png`/`grass_winter.png` reference (`config.json`, `server.py` DEFAULT_CONFIG + initial `_terrain_texture`, `generate_chunk.py`, test fixtures/assertions, docs), removed the old grass texture files. `chunk.py`'s `GRASS` block constant/save-format name intentionally left untouched — purely a visual/texture change, not a data-model change.
14. Added decorative grass patches as a proper vegetation type: `GRASS_PATCH = 5` in `chunk.py` (with a `_DEFAULT_VEGETATION_AGE` lookup table replacing the old hardcoded FLOWER/BUSH/TREE conditional chain in `set_block`/`load`, so new block types plug in without touching that logic again), and a new `"grass"` entry in `entities.json` (single stage, height 0.3/width 1.0, no loot, tried last in spawn priority so flower/bush/tree keep first claim on open tiles). `generate_chunk.py` now covers every remaining bare-soil tile with grass unconditionally after placing trees/bushes/flowers (100% at world-gen). At runtime, `_sim_step`'s spawn loop gained a generic `spawn.active_seasons` gate (checked before the chance roll, skips the whole entry outside those seasons) — grass uses `["spring", "summer"]` so it stops regrowing in fall/winter; entries without the field are unaffected. New `textures/grass_xcross_64.png` (procedural blade tuft, 64×64 cross billboard). 8 new tests (143 total), one existing spawn-priority test updated (grass now legitimately wins where nothing used to spawn, since it has no proximity blockers), verified live via a running server + inspector page (8833 grass patches on a fresh 100×100 world).

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
