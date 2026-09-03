# MidTerraSim

A client/server 3D ecosystem simulation built with [Ursina Engine](https://www.ursinaengine.org/). A headless **server** owns the authoritative world and runs the simulation continuously; a first-person **UI client** connects to it over HTTP, renders the world, and lets you walk around. The two are independent processes — you can start, stop, and restart the UI without touching the running simulation.

## Architecture

```
┌─────────────────┐   GET /health          ┌──────────────────────────┐
│                 │   GET /state           │                          │
│  main.py (UI)   │ ─────────────────────> │  server.py (headless)    │
│  Ursina client  │ <───────────────────── │  authoritative world +   │
│  no simulation  │   JSON snapshot         │  every simulation timer  │
│                 │   POST /save (manual)   │                          │
└─────────────────┘                        └──────────────────────────┘
```

- **`server.py`** — no Ursina import, console-only. Owns the `Chunk`, all simulation timers (vegetation age/spawn, seasons, day/night, creature movement/needs/feeding/sleep/lifecycle/reproduction, item drops + expiry), and persistence. Keeps simulating whether or not any client is connected. Exposes a small stdlib-only HTTP/JSON API bound to `127.0.0.1` by default.
- **`main.py`** — Ursina UI/client only. Polls the server's `/state` endpoint on a background thread (so a slow/offline server never freezes rendering), and rebuilds/updates visual entities (terrain, vegetation, creatures, drops) from the snapshot. Runs local first-person controls, camera, chunk-bound clamping, day/night visuals, and the HUD. If the server is unreachable it shows a **DISCONNECTED** banner and keeps retrying automatically; it reconnects on its own once the server is back — no restart of either process needed. Closing the UI (Esc or window close) never saves or shuts down the server.

## Running

Start the server first, in its own console:

```
python server.py
```

Then, independently, start the UI client (any number of times — start it, close it, start it again — the server doesn't care):

```
python main.py
```

- The server keeps ticking and saving even with zero clients connected.
- Closing `main.py` (Esc, or the window's close button) only closes the UI. It does **not** save or stop the server.
- Stop the server with **Ctrl+C** in its console — it saves the world before exiting.
- CLI overrides are available on both: `python server.py --host 0.0.0.0 --port 8765`, `python main.py --host 127.0.0.1 --port 8765`.

World state is saved to `chunks/chunk_0_0.wrld` periodically (`server.save_interval` in `config.json`), on `POST /save`, and on clean shutdown. That includes **flora, fauna, and the simulation clock**: every creature's tile, age, hunger, sleep state and `id`, plus the current season, cycle counter and day number. A restarted server therefore picks up the same population, in the same season, where it left off rather than reseeding a fresh spring world. Item drops are *not* persisted (their lifetime is tied to simulation time), and the day/night phase isn't either — it's derived from the wall clock.

The save format is at `version: 2` (see [`chunk.py`](./chunk.py)'s module docstring for the full JSON shape). Older `version: 1` files still load — they simply have no fauna or clock to restore, so the server seeds a fresh population and starts at spring, day 0.

To regenerate a fresh world with procedural textures:

```
python generate_chunk.py
```

To inspect a saved world from above:

```
python map_viewer.py
```

## Controls (UI client)

| Key | Action |
|-----|--------|
| WASD | Move |
| Mouse | Look |
| Shift | Sprint |
| Space | Jump |
| Esc | Close the UI (server keeps running) |

### Player and world heights (client-side)

The server only reports a single `surface_y` (`SY`); every ground-relative
height is the client's business, and they all derive from one helper each so
they can't drift apart:

| Thing | Y |
|-------|---|
| Visual ground, plant bases, grass decal | `surface_top(SY)` = `SY + 0.5` |
| Walkable floor top (player's feet) | `floor_top(SY)` = `SY + 0.6` |
| Camera / eyes | feet + `PLAYER_EYE_HEIGHT` (2), i.e. normal bush height |

The floor collider is a thick slab (`FLOOR_THICKNESS`) rather than a thin
one, and the player spawns with their feet already on it. Both matter: Ursina's
gravity step scales with frame time, and the frame that builds the terrain and
vegetation meshes is slow enough that a single step could otherwise skip a thin
collider and drop the player into the void. `update()` also snaps the player
back up if they ever end up below the floor. See `takeover.md`'s **Gotchas**.

## Running Tests

The `server.py` simulation engine (and `chunk.py`'s data model) have an automated test suite under [`tests/`](./tests). It runs entirely offline, against isolated temp-file fixtures — it never touches your real `config.json`, `entities.json`, or `chunks/chunk_0_0.wrld`.

```
pip install -r requirements-dev.txt
python -m pytest
```

With coverage:

```
python -m pytest --cov=server --cov=chunk --cov-report=term-missing
```

Current coverage: **100%** on `chunk.py`, **95%** on `server.py` (the only uncovered lines are `server.py`'s `main()` CLI entrypoint/tick-loop, which isn't practical to exercise as a unit test, plus one pre-existing unreachable defensive branch).

What's covered:
- Config loading/merging/hot-reload (`load_config`, `maybe_reload_config`)
- World seeding, tag/diet/avoidance resolution, vegetation stage lookup
- Fauna persistence: saving live creatures into the world file, restoring them on load, keeping ids unique, and seeding any species the file doesn't know about
- Clock persistence: resuming season/cycle/day, falling back when a saved season is no longer in `config.json`, and seeding the day/night edge from the current phase
- Item drops: spawn, stage-specific loot, expiry, and pickup (including diet/sleep gating)
- Flower attack/eat-in-place feeding
- Herbivore plant diets (`_resolve_plant_diet`, `_act_feed_plants`): grazing ground cover, browsing bushes, and seeking the nearest plant within `feed_radius`
- `feed_radius` resolution (`_feed_radius`) and its effect on both the carnivore and herbivore search paths
- Creature pathfinding primitives (`_step_toward`, `_find_nearest_*`, `_move_creature_random`)
- Creature needs/sleep state machine (`_compute_creature_needs`, `_pick_highest_need`, `_act_feed`, `_creature_move`)
- Daily/seasonal lifecycle (hunger/age decay, winter aging, reproduction each season change, removal)
- The flora simulation cycle (`_sim_step`): decay, season rotation, tag-driven spawn-blocking rules, and season-gated spawning (`spawn.active_seasons`, used by grass)
- `tick()` scheduling: day/night transitions, revision counting, periodic save/sim-step triggers, creature movement/sleep timing, and `speed_multiplier` scaling
- The admin `speed_multiplier` control (`set_speed_multiplier`, `admin_state`, `_effective_time`) — validation, and its no-op guarantee at the default value of `1`
- The full HTTP/JSON API (`/health`, `/state`, `/save`, `/admin`, `/admin/speed_multiplier`, unknown routes, unsupported methods, concurrent requests, and the `/` inspector page) against a real `ThreadingHTTPServer`

## HTTP API (`server.py`)

`server.py` exposes a small stdlib-only HTTP/JSON API, bound to
`127.0.0.1:8765` by default (see `server` section of `config.json`):

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Human-facing HTML debug page — a collapsible tree of vegetation/creatures/drops, plus the admin speed control (see below) |
| `/health` | GET | `{"status": "ok", "revision": <int>}` — cheap liveness/poll check |
| `/state`  | GET | Full renderable world snapshot (terrain/time, vegetation, creatures, drops) |
| `/admin`  | GET | Current admin runtime state: `{"speed_multiplier": <int>}` |
| `/save`   | POST | Force an immediate save to `chunks/chunk_0_0.wrld` |
| `/admin/speed_multiplier` | POST | Set the runtime speed multiplier, `{"value": <int 1-100>}` |

Open `http://127.0.0.1:8765/` (or your configured host/port) in any browser
for a live, self-contained inspector page — no build step, no external
assets. It polls `/state` once per second and renders everything as a
collapsible tree, grouped by type, with each creature broken out to show its
raw stats and the server's currently-computed `needs`. Grass patches are
omitted from the Vegetation section (they cover most of the map and drown out
flowers/bushes/trees); they remain in `/state` for the UI client.

```
▾ Vegetation (1140)
    ▸ bush (474)
    ▸ flower (454)
    ▸ tree (212)
▾ Creatures (10)
  ▾ rabbit (5)
    ▸ rabbit #1 ... #5
  ▾ rat (5)
      ▾ rat #1
          position: (13, 88)  age: 2  hunger: 3  sleep: 0  asleep: false
        ▾ needs
            feed: 0
            sleep: 0
      ▸ rat #2 ... #5
▸ Drops (0)
```

An **Admin** panel above the tree lets you change the simulation's
`speed_multiplier` (`1`–`100`) directly in the browser — useful for testing,
since at the default `1x` a season change (`season_length × cycle_length`
seconds) can take tens of minutes in real time. Setting it to e.g. `20`
speeds up day/night, creature movement/needs, vegetation growth/spawn,
season rotation, saves, and item-drop aging all together by that factor;
setting it back to `1` returns to real-time. It's transient admin state —
not saved to `config.json`, always resets to `1` on server restart.

All snapshot reads and mutations are guarded by a single lock, so concurrent
client requests never race with the simulation tick — `main.py` is just one
possible client; anyone can poll this API from another process or language.

**➡️ For the full contract** — exact JSON schema and field semantics,
request/response headers and status codes, thread-safety guarantees,
`entities.json` texture/stage resolution, coordinate assumptions, polling
and reconnect recommendations, curl/PowerShell/Python examples, and
known limitations — see **[`SERVER_CLIENT_API.md`](./SERVER_CLIENT_API.md)**.

## Configuration

### `config.json`

Hot-reloaded by the **server** every tick (no restart needed for `cycle_length`, `season_length`, `day_night_cycle`, `drop_lifetime`, `seasons`). The `client` section is read once by `main.py` at startup.

```json
{
  "cycle_length": 300.0,
  "season_length": 10,
  "day_night_cycle": 60.0,
  "drop_lifetime": 60.0,
  "seasons": {
    "spring": { "moisture": 40, "fertility": 20, "texture": "soil.png" },
    "summer": { "moisture": 20, "fertility": 30, "texture": "soil.png" },
    "fall":   { "moisture": 30, "fertility": 40, "texture": "soil_fall.png" },
    "winter": { "moisture": 30, "fertility": 10, "texture": "soil_winter.png" }
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8765,
    "tick_rate": 20.0,
    "save_interval": 120.0
  },
  "client": {
    "host": "127.0.0.1",
    "port": 8765,
    "poll_interval": 0.15,
    "request_timeout": 2.0
  }
}
```

| Key | Description |
|-----|-------------|
| `cycle_length` | Seconds between simulation ticks (flora aging/spawn) |
| `season_length` | Simulation cycles per season |
| `day_night_cycle` | Seconds for one full day/night loop |
| `drop_lifetime` | Seconds before uncollected item drops expire |
| `server.host` / `server.port` | Where the HTTP API binds (default localhost-only) |
| `server.tick_rate` | Simulation ticks per second (independent of any client) |
| `server.save_interval` | Seconds between automatic saves |
| `client.host` / `client.port` | Where `main.py` looks for the server |
| `client.poll_interval` | Seconds between `/state` polls |
| `client.request_timeout` | HTTP request timeout for the polling thread |

### `entities.json`

Defines items, vegetation, and creatures. Loaded independently by **both** `server.py` (authoritative rules) and `main.py` (rendering/texture lookups) — unchanged from before.

**Items** — `name`, `tags` (e.g. `raw`, `food` for seed/berry/meat)

**Vegetation** — `tags`, `block_id`, `stages[]` (texture, optional `render`/`height`/`width`, per-stage loot), `spawn` rules. Stage `render` defaults to `"cross"` (vertical billboard); `"surface"` lays a flat texture on the tile top (used by grass).

**Creatures** — `needs`, `diet`, hunger/age, movement, reproduction, death loot.
`diet` entries are resolved against both items and vegetation, by name or by
tag: matching **items** make a carnivore/scavenger that eats drops and attacks
flowers (rat), while matching **vegetation** makes a herbivore that grazes and
browses plants (rabbit). Diet order is preference order.

`feed_radius` caps how far a hungry creature will look for its food, in
Manhattan tiles, and applies to *both* paths — drop/flower search for
carnivores, plant search for herbivores. It defaults to
`server.DEFAULT_FEED_RADIUS` (5) when a definition omits it, but both shipped
creatures declare it explicitly (rat 5, rabbit 6).

Block IDs (`chunk.py`): `AIR=0`, `GRASS=1` (bare soil), `FLOWER=2`, `BUSH=3`, `TREE=4`, `GRASS_PATCH=5`

Creature definitions are matched to saved fauna **by `name`**, so reordering
`entities.json` is safe, and a species the world file has never seen is seeded
fresh on load instead of requiring a world regeneration.

#### Spawn priority (runtime)

On each eligible tile, flora are tried in file order: **flower → bush → tree → grass**. First successful roll wins.

Eligible tiles:

- **Flower / bush / tree** — bare soil **or** a grass patch (ground cover is a soft under-layer; a plant may claim and replace it)
- **Grass** — bare soil only (will not stack on an existing grass patch)

Flower/bush/tree never treat grass as an occupying blocker. Grass also has no proximity rules against other flora.

| Plant | Chance | Constraints |
|-------|--------|-------------|
| Flower | 9% | Max 1 neighbor flower within radius 1 |
| Bush | 3% | No tree/bush within radius 1 |
| Tree | 1% | No tree within radius 2, no bush within radius 1 |
| Grass | 15% | Only during `active_seasons` (spring, summer) — no proximity constraints |

All spawns also require a fertility roll (`random 0–100 ≤ chunk.fertility`). A
vegetation entry's `spawn.active_seasons` (optional, e.g. `["spring", "summer"]`)
gates the whole entry to specific seasons — used by `grass` so bare ground
stops regrowing grass in fall/winter, but any entry can use it.

Grass itself is decorative ground cover rendered as a flat surface texture
(`stage.render: "surface"` + `textures/grass.png`) laid over the soil tile:
`generate_chunk.py` covers every bare-soil tile with it at world-gen time
(100%, unconditional), while the runtime spawn loop above only re-grows it
on bare soil (and never blocks flower/bush/tree from claiming a grassy tile).
It decays like any other `flora`-tagged vegetation (moisture-driven).

#### Stage-based loot (on death)

| Plant | Stage | Drops |
|-------|-------|-------|
| Flower | Dead (age ≤ 1) | 1–3 seeds |
| Flower | Live | 0–1 seeds |
| Bush | Small (age 5+) | 0–1 stick |
| Bush | Normal (age 2–4) | 1–2 berry, 3–4 sticks |
| Bush | Dead (age ≤ 1) | 3–4 berry, 3–4 sticks |
| Tree | Small (age 9+) | 3–4 sticks |
| Tree | Normal/medium | 2–4 logs, 4–6 sticks |
| Tree | Dead (age ≤ 1) | 3–5 logs, 5–7 sticks |
| Grass | — | none |
| Rat | — | 1 meat |
| Rabbit | — | 2–3 meat |

## Simulation Overview (server-authoritative)

Each simulation cycle (`cycle_length` seconds):

1. Season may advance, updating moisture, fertility, and ground texture
2. Flora ages and may die when moisture is low; dead plants spawn stage-based item drops
3. New flora may spawn (fertility roll + per-type chance + proximity rules). Grass patches do not block flower/bush/tree — those may claim a grassy tile.
4. Fauna creatures act on needs-driven tasks or move randomly (evaluated every `move_interval_day` seconds)

### Creature feed AI (rats)

When hungry (`feed` need = `initial_hunger - hunger`):

1. Eat food drops on same tile (seed, berry, meat — resolved via `food` tag)
2. Attack flower on same tile (reduce age by `attack`)
3. Move toward nearest food drop within `feed_radius` (5 for rats)
4. Move toward nearest dead flower, then live flower — same `feed_radius`
5. Random move if nothing found

### Creature feed AI (rabbits)

Herbivore diet (`diet: ["grass", "bush"]`, search radius `feed_radius`, 6 for rabbits):

1. If standing on grass cover — eat it (tile returns to bare soil), +1 hunger
2. Else move toward nearest grass within `feed_radius`
3. Else if standing on a bush — browse it (bush age −`attack`, +1 hunger); bush dies and drops loot at age 0
4. Else move toward nearest bush within `feed_radius`
5. Random move if nothing found

### Creature sleep AI (rats / rabbits)

Sleep is a plain need value with **no threshold** — it just competes with other needs for priority:

- Each night movement cycle a creature is awake, `sleep` increases by `sleep_gain` (default `0.5`)
- Whichever need (`feed` or `sleep`) has the higher value wins; if `sleep` wins, the creature stops moving, is marked `asleep`, and the client renders it with `sleep_texture`
- At the start of each day, `sleep` resets to `0`, `asleep` clears, and the client renders the normal `texture` again

Daily and seasonal events:

- **Day start** — fauna lose 1 hunger (or 1 age if starving); sleeping fauna wake up
- **Every season start** — fauna reproduce near existing individuals (`reproduce_count`)
- **Winter start** — all fauna lose 1 age first, then survivors reproduce

## Project Structure

```
MidTerraSim/
├── server.py          # Headless simulation server: authoritative state, all timers, HTTP API, persistence
├── main.py            # Ursina UI client: rendering, player controls, HUD — no simulation logic
├── chunk.py           # World data model and persistence (shared by server + generate_chunk.py)
├── generate_chunk.py  # Procedural world and texture generation
├── map_viewer.py      # Top-down .wrld file inspector (Tkinter)
├── config.json        # Runtime simulation timing, seasons, and server/client host/port/poll options
├── entities.json      # Items, vegetation, and creature definitions (read by both processes)
├── SERVER_CLIENT_API.md  # Full HTTP/JSON API contract for building your own client
├── takeover.md        # Session handoff notes for continuing development
├── roadmap.md         # Planned future work / backlog
├── tests/             # Automated test suite for server.py + chunk.py
├── pytest.ini         # pytest discovery configuration
├── requirements-dev.txt  # Test-only dependencies (pytest, pytest-cov)
├── chunks/            # Saved world state (.wrld)
└── textures/          # PNG assets (16×16 and 64×64 variants)
```

## Roadmap

Planned but not-yet-implemented work (new creatures, flora variety, terrain
generation, water, crops, and more) is tracked in
[`roadmap.md`](./roadmap.md).
