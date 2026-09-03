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

- **`server.py`** — no Ursina import, console-only. Owns the `Chunk`, all simulation timers (vegetation age/spawn, seasons, day/night, creature movement/needs/feeding/sleep/nesting/lifecycle/reproduction, structure weathering, item drops + expiry), and persistence. Keeps simulating whether or not any client is connected. Exposes a small stdlib-only HTTP/JSON API bound to `127.0.0.1` by default.
- **`main.py`** — Ursina UI/client only. Polls the server's `/state` endpoint on a background thread (so a slow/offline server never freezes rendering), and rebuilds/updates visual entities (terrain, vegetation, structures, creatures, drops) from the snapshot. Runs local first-person controls, camera, chunk-bound clamping, day/night visuals, and the HUD. If the server is unreachable it shows a **DISCONNECTED** banner and keeps retrying automatically; it reconnects on its own once the server is back — no restart of either process needed. Closing the UI (Esc or window close) never saves or shuts down the server.

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

World state is saved to `chunks/chunk_0_0.wrld` periodically (`server.save_interval` in `config.json`), on `POST /save`, and on clean shutdown. That includes **flora, fauna, structures, and the simulation clock**: every creature's tile, age, hunger, sleep state, `home`, whatever it's `carrying` and its `id`, every burrow's tile/age/larder, plus the current season, cycle counter and day number. A restarted server therefore picks up the same population, in the same season, where it left off rather than reseeding a fresh spring world. Item drops are *not* persisted (their lifetime is tied to simulation time), and the day/night phase isn't either — it's derived from the wall clock.

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

The `server.py` simulation engine (and `chunk.py`'s data model) have an automated test suite under [`tests/`](./tests). It runs entirely offline, against isolated temp-file fixtures — it never touches your real `config.json` or `chunks/chunk_0_0.wrld`.

The one exception is [`tests/test_entities_file.py`](./tests/test_entities_file.py), which reads the *real* `entities.json` (read-only) and checks it for self-consistency: every referenced texture exists on disk, loot tables only name declared items, block ids are unique and known to `chunk.py` with matching default ages, stages are in ascending `max_age` order, and no creature diet entry is ambiguous between a plant and an item. These are the failures that otherwise surface as a brown untextured quad or a crash at the moment some plant happens to die.

```
pip install -r requirements-dev.txt
python -m pytest
```

With coverage:

```
python -m pytest --cov=server --cov=chunk --cov-report=term-missing
```

Current coverage: **100%** on `chunk.py`, **96%** on `server.py` (the uncovered lines are `server.py`'s `main()` CLI entrypoint/tick-loop, which isn't practical to exercise as a unit test, plus a handful of defensive guards and loop short-circuits that duplicate a check a caller has already made).

What's covered:
- Config loading/merging/hot-reload (`load_config`, `maybe_reload_config`)
- World seeding, tag/diet/avoidance resolution, vegetation stage lookup
- Fauna persistence: saving live creatures into the world file, restoring them on load, keeping ids unique, and seeding any species the file doesn't know about
- Clock persistence: resuming season/cycle/day, falling back when a saved season is no longer in `config.json`, and seeding the day/night edge from the current phase
- Item drops: spawn, stage-specific loot, expiry, and pickup (including diet/sleep gating, and leaving a stack alone when the creature is full or only ate part of it)
- Flower attack/eat-in-place feeding
- Herbivore plant diets (`_resolve_plant_diet`, `_act_feed_plants`): grazing ground cover, browsing bushes, and seeking the nearest plant within `feed_radius`
- `feed_radius` resolution (`_feed_radius`) and its effect on both the carnivore and herbivore search paths
- Creature pathfinding primitives (`_step_toward`, `_find_nearest_*`, `_move_creature_random`)
- Creature needs/sleep state machine (`_compute_creature_needs`, `_ranked_needs`, `_act_feed`, `_creature_move`), including a task declining its turn and handing over to the next need
- Structures: the `home` need accruing while homeless, building/adopting a burrow (`_act_home`), one structure per tile, seasonal weathering and collapse (`_break_structures`), eviction of dwellers, and structure persistence including dangling-`home` cleanup
- Stocking (`_act_stock`, `_act_deliver`): need ordering against hunger and shelter, taking one item off a stack, hauling it home ignoring all other needs, stashing/stacking it in the larder, and putting it back on the ground when the burrow or the creature is lost
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
| `/` | GET | Human-facing HTML debug page — a collapsible tree of vegetation/structures/creatures/drops, plus the admin speed control (see below) |
| `/health` | GET | `{"status": "ok", "revision": <int>}` — cheap liveness/poll check |
| `/state`  | GET | Full renderable world snapshot (terrain/time, vegetation, structures, creatures, drops) |
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
▾ Vegetation (2924)
    ▸ bush (465)
    ▸ cabbage (860)
    ▸ carrot (924)
    ▸ flower (469)
    ▸ tree (206)
▾ Structures (2)
  ▾ burrow (2)
      ▾ burrow #1
          position: (7, 11)  age: 2  dwellers: rat #3  contains: 2x berry, 1x seed
      ▸ burrow #2
▾ Creatures (10)
  ▾ rabbit (5)
    ▸ rabbit #1 ... #5
  ▾ rat (5)
      ▾ rat #1
          position: (13, 88)  age: 2  hunger: 3  sleep: 0  asleep: false
          home: #1  carrying: berry
        ▾ needs
            feed: 0
            sleep: 0
            home: 0
            stock: 0.9
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

Defines items, vegetation, structures, and creatures. Loaded independently by **both** `server.py` (authoritative rules) and `main.py` (rendering/texture lookups) — unchanged from before.

**Items** — `name`, `tags` (e.g. `raw`, `food` for seed/berry/meat/carrot/cabbage)

**Vegetation** — `tags`, `block_id`, `initial_age`, `age_decay_every_n_cycles`, `stages[]` (texture, optional `render`/`height`/`width`, per-stage loot), `spawn` rules. Stage `render` defaults to `"cross"` (vertical billboard); `"surface"` lays a flat texture on the tile top (used by grass). `block_id` must also be listed in `chunk.py` (`BLOCK_NAMES` and the default-age table used when backfilling older save files).

**Structures** — things creatures build. `texture`, `render` (`"surface"`),
`initial_age` (how many seasonal hits it can take), `break_chance` (per-season
odds of taking one), `dwellers` (which creatures may live in it, by name or
tag), and `contains` (the dwellers' larder, which the `stock` need fills with
`{"item", "count"}` entries). Structures are *not* blocks: they live on a tile
alongside whatever vegetation is there and are drawn over it.

**Creatures** — `needs`, `diet`, hunger/age, movement, reproduction, death loot.
`diet` entries are resolved against both items and vegetation, by name or by
tag: matching **items** make a carnivore/scavenger that eats drops and attacks
flowers (rat), while matching **vegetation** makes a herbivore that grazes and
browses plants (rabbit). Diet order is preference order.

`feed_radius` caps how far a creature will look for food, in Manhattan tiles,
and applies to *all* search paths — drop/flower search for carnivores, plant
search for herbivores, and the hunt for something to hoard. It defaults to
`server.DEFAULT_FEED_RADIUS` (5) when a definition omits it, but both shipped
creatures declare it explicitly (rat 5, rabbit 6).

`stock_need` is the fixed priority of the `stock` need (`0.9`, or
`server.DEFAULT_STOCK_NEED`); `home_gain` (`0.5`) is how much wanting a home
grows per day while homeless.

Block IDs (`chunk.py`): `AIR=0`, `GRASS=1` (bare soil), `FLOWER=2`, `BUSH=3`, `TREE=4`, `GRASS_PATCH=5`

Creature definitions are matched to saved fauna **by `name`**, so reordering
`entities.json` is safe, and a species the world file has never seen is seeded
fresh on load instead of requiring a world regeneration. Saved structures are
matched by `type` the same way; a structure whose type no longer exists in
`entities.json` is dropped on load rather than kept as an unrenderable ghost.

#### Spawn priority (runtime)

On each eligible tile, flora are tried in file order: **flower → bush → tree → carrot → cabbage → grass**. First successful roll wins.

Eligible tiles:

- **Flower / bush / tree / crops** — bare soil **or** a grass patch (ground cover is a soft under-layer; a plant may claim and replace it)
- **Grass** — bare soil only (will not stack on an existing grass patch)

Only grass is a soft under-layer; every other plant blocks the tile against the rest. Grass has no proximity rules against other flora.

| Plant | Chance | Constraints |
|-------|--------|-------------|
| Flower | 9% | Max 1 neighbor flower within radius 1 |
| Bush | 3% | No tree/bush within radius 1 |
| Tree | 1% | No tree within radius 2, no bush within radius 1 |
| Carrot | 4% | None — crops may grow shoulder to shoulder |
| Cabbage | 4% | None — crops may grow shoulder to shoulder |
| Grass | 15% | Only during `active_seasons` (spring, summer) — no proximity constraints |

An unconstrained 4% is dense: measured over 30 cycles on a 100×100 map, the
two crops settle around 900 tiles each (~18% of the map between them), mostly
colonizing bare soil — the other flora lose about 10–14% of their standing
count to the competition. Give them a `max_same_within` (as flower has) to
thin them into patches rather than fields.

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

#### Crops, and how long a plant lives

Carrot and cabbage are `crops`-tagged flora: a single stage, no proximity
rules, and one item — a carrot or a cabbage — dropped when the plant dies.
Both are `raw`/`food` items, which puts them straight on the rat's menu (its
diet is the `food` tag), so rats will eat them off the ground and haul them
into their burrows.

Lifetime is the product of two numbers rather than a duration. Every cycle a
plant whose `current_cycle % age_decay_every_n_cycles == 0` rolls against
moisture and loses 1 age on success; at age `0` it dies and drops its loot.
So the expected life is roughly `initial_age × age_decay_every_n_cycles ÷ P`,
where `P` (about 0.7 at the seasonal average moisture) is the chance of any
one roll landing. Measured over 40,000 simulated plants, with `season_length`
at 10 cycles:

| Plant | `initial_age` | decay every | Mean life | In seasons |
|-------|---------------|-------------|-----------|------------|
| Flower | 2 | 1 cycle | ~2.6 cycles | ~0.3 |
| Carrot / cabbage | 2 | 4 cycles | ~10.1 cycles | ~1.0 |
| Bush | 5 | 2 cycles | ~14 cycles | ~1.4 |
| Grass | 5 | 3 cycles | ~21 cycles | ~2.1 |
| Tree | 10 | 2 cycles | ~29 cycles | ~2.9 |

The spread is wide, which is the point: crops have a 10th percentile of ~5
cycles and a 90th of ~16, so a field sown together doesn't come up all at
once. Note that a low `age` means *old* — age counts **down** from
`initial_age`, so a crop is young at 2 and ripe at 1.

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
| Carrot | any (single stage) | 1–2 carrots |
| Cabbage | any (single stage) | 1–2 cabbages |
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

1. Eat food drops on same tile (seed, berry, meat, carrot, cabbage — resolved via `food` tag)
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

### Creature stock AI (rats)

A fed creature with a burrow hoards food in it. Unlike the other needs,
`stock` has a **constant** value (`stock_need`, `0.9`) — hoarding is never
satisfied and never urgent, so the number exists purely to place it in the
pecking order: below any real hunger (`feed` is ≥ 1 the moment a creature is
hungry at all) and above a `home` want that hasn't had two days to build up.

When `stock` comes up:

1. If the creature has no standing burrow, it declines the turn
2. If an edible drop is on its tile, it takes **one** item from it (the rest of the stack stays on the ground), and that item becomes its `carrying`
3. Else it steps toward the nearest edible drop within `feed_radius`
4. If there's nothing in reach, it declines the turn

Declining matters: needs are ranked by value and tried in order, so a turn a
task passes on falls through to the next need rather than wasting the tick.

**While carrying, needs are not evaluated at all** — a loaded creature won't
stop to eat, sleep or dig. It walks to its burrow, and on arrival the item
moves from `carrying` into the burrow's `contains` (stacking by item name).
If the burrow collapses mid-haul, or the creature dies, the item is put back
on the ground rather than vanishing. Nothing draws on a stocked larder yet.

Rabbits declare the need but never act on it: their diet resolves to
vegetation rather than items, so there's nothing carryable for them to fetch
and the turn always falls through.

### Creature home AI (rats / rabbits)

Rats and rabbits both dwell in **burrows**. A creature's `home` is the id of
the structure it lives in, or empty while it has none:

- Each **day start** a homeless creature's `home` need grows by `home_gain` (`0.5`); housed creatures don't accrue it, and their `home` need reads `0`
- When `home` wins the needs contest, the creature claims the tile it is standing on: it adopts the burrow already there if it's a listed `dwellers` type, otherwise it digs a new one. Only one structure may occupy a tile.
- On success its `home` is set to that burrow's id and the accrued need resets to `0`. Several creatures may share one burrow.
- Burrows override the tile's look (drawn above both soil and grass) but don't block anything — vegetation keeps growing on the same tile

Because needs simply compete by value, a starving creature keeps feeding
(`feed` grows by 1/day) before it bothers digging (`home` grows by 0.5/day).
Building happens once it's fed.

Daily and seasonal events:

- **Day start** — fauna lose 1 hunger (or 1 age if starving); sleeping fauna wake up; homeless fauna want a home a bit more
- **Every season start** — each structure has a `break_chance` (20% for burrows) of losing 1 age; at age 0 it collapses and its dwellers are evicted back to homeless. Then fauna reproduce near existing individuals (`reproduce_count`).
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
├── entities.json      # Items, vegetation, structure, and creature definitions (read by both processes)
├── SERVER_CLIENT_API.md  # Full HTTP/JSON API contract for building your own client
├── takeover.md        # Session handoff notes for continuing development
├── roadmap.md         # Planned future work / backlog
├── tests/             # Automated test suite for server.py + chunk.py, plus entities.json validation
├── pytest.ini         # pytest discovery configuration
├── requirements-dev.txt  # Test-only dependencies (pytest, pytest-cov)
├── chunks/            # Saved world state (.wrld)
└── textures/          # PNG assets (16×16 and 64×64 variants), referenced by name from entities.json
```

## Roadmap

Planned but not-yet-implemented work (new creatures, flora variety, terrain
generation, water, crops, and more) is tracked in
[`roadmap.md`](./roadmap.md).
