# MidTerraSim Server ↔ Client API

This is the standalone contract for the [`server.py`](./server.py) headless simulation
HTTP/JSON API. Read this if you want to build **your own client** (in any
language) against a running MidTerraSim server, instead of — or in addition
to — the bundled Ursina UI client, [`main.py`](./main.py).

Everything documented here reflects what [`server.py`](./server.py) actually implements
today (verified against a running instance). It is intentionally a small,
polling-based, read-mostly API — there is no authentication, no streaming
transport, and (with one exception) no way to mutate the simulation.

For project setup, controls, and configuration reference, see [`README.md`](./README.md).
For internal design notes and history, see [`takeover.md`](./takeover.md).

---

## 1. Purpose

`server.py` owns the **authoritative** world/ecosystem state — terrain,
vegetation, creatures, item drops, seasons, and day/night — and runs every
simulation timer whether or not any client is connected. It exposes that
state to clients over a minimal stdlib-only HTTP/JSON API so that rendering,
tooling, dashboards, bots, or automated tests can be built as separate,
disposable processes with **no simulation logic of their own**.

The server and any client are fully independent processes:

- The server keeps ticking, aging vegetation, moving creatures, and saving to
  disk with zero clients connected.
- A client can start, stop, crash, or reconnect at any time without
  affecting the simulation — the server never blocks on client I/O and holds
  no per-client session state.
- Multiple clients (or tools) can read from the same server concurrently.

## 2. Transport

- **Protocol:** plain HTTP, JSON request/response bodies. Requests are
  ordinary HTTP/1.1 requests (as issued by `curl`, `urllib`, browsers, etc.);
  the server itself is a `http.server.BaseHTTPRequestHandler`-based handler
  that has not overridden `protocol_version`, so it answers each request as
  `HTTP/1.0` and **closes the connection after every response** — there is no
  keep-alive/persistent connection. Clients should open a new (or
  short-lived) HTTP connection per request; standard HTTP client libraries
  handle this transparently.
- **Default address:** `127.0.0.1:8765` (localhost-only by default — see
  §12 for the security implications of changing this).
- **Content type:** all responses are `application/json`, UTF-8 encoded, with
  a compact (no extra whitespace) `json.dumps(...)` body.
- **Concurrency:** the server is a `ThreadingHTTPServer` — it can serve
  multiple simultaneous connections — but every request handler is
  serialized behind a single lock together with the simulation tick (see
  §7).

## 3. Starting the server & host/port overrides

```
python server.py
python server.py --host 0.0.0.0 --port 8765
```

- `--host` / `--port` are optional CLI flags (via `argparse`). If omitted,
  the server binds to `server.host` / `server.port` from [`config.json`](./config.json)
  (default `127.0.0.1` / `8765`).
- On startup the server requires an existing world file
  (`chunks/chunk_0_0.wrld`); if it's missing, the process prints an error and
  exits — run `python generate_chunk.py` first.
- The server prints `[server] listening on http://<host>:<port>` once the
  HTTP thread is up, then ticks the simulation on its main thread until
  `Ctrl+C`, saving on clean shutdown.

There is no server-side flag to change the API schema or endpoint set — the
three routes below are fixed.

## 4. Endpoint reference

| Endpoint | Method | Success | Description |
|----------|--------|---------|--------------|
| `/` | GET | `200` | Human-facing HTML debug page — a self-contained, dependency-free tree view of the world, plus the admin speed-multiplier control (see §4a). Not part of the JSON API contract. |
| `/health` | GET | `200` | Cheap liveness/poll check |
| `/state`  | GET | `200` | Full renderable world snapshot |
| `/admin`  | GET | `200` | Current admin-controlled runtime state (see §4b) |
| `/save`   | POST | `200` | Force an immediate save to disk |
| `/admin/speed_multiplier` | POST | `200`/`400` | Set the runtime simulation speed multiplier (see §4b) |
| anything else | GET/POST | `404` | `{"error": "not found"}` |
| any route | any other HTTP method (`PUT`, `DELETE`, `HEAD`, …) | `501` | Unsupported method (default `BaseHTTPRequestHandler` behavior; no JSON body — `do_GET`/`do_POST` are the only handlers defined) |

### Request headers

No custom or required request headers. No request body is read or expected
for `GET` routes or `POST /save`; anything you send as a body to those is
ignored. `POST /admin/speed_multiplier` is the one route that *does* read a
JSON request body — see §4b.

### Response headers

Every JSON response includes:

- `Content-Type: application/json`
- `Content-Length: <n>`
- Standard `BaseHTTPRequestHandler` headers: `Server: MidTerraSim/1.0
  Python/<version>` and `Date: <RFC 1123 timestamp>`.

### 4a. `GET /` — the HTML inspector page

Returns a self-contained HTML+CSS+JS page (no external assets, no CDN
dependencies, works fully offline) that polls `GET /state` client-side once
per second and renders a collapsible tree:

```
▾ Vegetation (1140)
    ▸ bush (474)
    ▸ flower (454)
    ▸ tree (212)
▾ Creatures (5)
  ▾ rat (5)
      ▾ rat #1
          position: (13, 88)
          age: 2
          hunger: 3
          sleep: 0
          asleep: false
        ▾ needs (2)
            feed: 0
            sleep: 0
      ▸ rat #2 ... #5
▸ Drops (0)
```

Vegetation and drops are grouped by `type`/`item`; creatures are grouped by
`type` and then broken out per-instance, each showing its raw stats plus a
nested `needs` group with the server's *currently computed* need values (see
`creatures[].needs` in §5). Expand/collapse state is preserved across the
1-second poll (tracked client-side by a stable `data-key` per node), so the
tree doesn't jump around while you're inspecting it.

Above the tree, an **Admin** panel shows the current `speed_multiplier` and
lets you change it (see §4b) without leaving the browser.

This route is a debugging convenience, not a stable API contract — its HTML
structure/styling may change between versions. Third-party clients should
use `GET /state` directly rather than scraping this page.

### 4b. Admin: `GET /admin`, `POST /admin/speed_multiplier`

A small, intentionally minimal admin surface for runtime testing controls.
`speed_multiplier` is the first (and currently only) such control — it's
**not** part of `config.json` and **not** persisted; it's in-memory only and
always resets to `1` on server restart.

**`GET /admin`** — current admin-controlled runtime state:

```json
{"speed_multiplier": 1}
```

**`POST /admin/speed_multiplier`** — set it. Requires a JSON request body:

```json
{"value": 10}
```

| Response | Status | Body |
|---|---|---|
| Accepted | `200` | `{"speed_multiplier": 10}` — echoes the new value |
| Missing/empty body, missing `value`, or malformed JSON | `400` | `{"error": "expected a JSON body: {\"value\": <int 1-100>}"}` |
| `value` outside `1`–`100`, or not coercible to `int` | `400` | `{"error": "speed_multiplier must be between 1 and 100"}` (or `"...must be an integer"`) |

`value` is coerced with Python's `int(...)`, so numeric strings like `"10"`
are accepted too. On rejection, `world.speed_multiplier` is left unchanged.

**What it does:** scales *every* time-based feature uniformly — day/night
progression, creature movement/needs ticks, the flora simulation cycle
(vegetation aging/spawn, season rotation), periodic saves, and item-drop
aging — all speed up together by the same factor. Internally the server
tracks a virtual-time offset added on top of the real wall clock; at the
default of `1` this offset never leaves `0`, so `phase`/drop `age` are
byte-for-byte identical to a server that has never touched `/admin` (see
§5's `time.phase` and `drops[].age` notes, updated below). There is
**no** way to *slow down* the simulation (minimum is `1`, i.e. real-time).

### `GET /health`

Returns immediately, guarded by the world lock only long enough to read the
current revision counter:

```json
{"status": "ok", "revision": 418}
```

| Field | Type | Meaning |
|-------|------|---------|
| `status` | string | Always `"ok"` if the server responded at all |
| `revision` | int | Same monotonic tick counter as `/state`'s `revision` — lets you detect ticking/restarts without pulling the full snapshot |

### `GET /state`

Returns the full renderable snapshot described in §5. Always `200` on
success — there is no filtering, pagination, or partial-update variant; every
call returns the complete current world state.

### `POST /save`

Forces an immediate synchronous save of the chunk (terrain + vegetation
ages) to `chunks/chunk_0_0.wrld`, then returns:

```json
{"saved": true, "revision": 1234}
```

Note this does **not** bump `revision` itself — the returned `revision` is
whatever the simulation tick counter currently is. Saving does not pause or
otherwise affect the simulation; it can be called at any time.

### Unknown routes

Any path other than `/health`, `/state` (GET) or `/save` (POST) returns:

```
HTTP/1.0 404 Not Found
Content-Type: application/json

{"error": "not found"}
```

This includes typos, trailing slashes, query strings on unrelated paths, and
`GET /save` / `POST /health` / `POST /state` (the method+path combination
must match exactly one of the three routes above).

---

## 5. `/state` response schema

Exact shape as produced by `World.snapshot()`. All numeric IDs and counters
are plain JSON numbers (ints unless noted); all coordinates are integers.

```jsonc
{
  "revision": 1234,
  "vegetation_revision": 12,
  "chunk": {
    "size": [100, 100, 100],
    "surface_y": 99
  },
  "time": {
    "season": "spring",
    "cycle": 4,
    "day": 2,
    "is_day": true,
    "phase": 0.42,
    "day_night_cycle": 60.0
  },
  "terrain": {
    "texture": "textures/soil.png"
  },
  "vegetation": [
    { "x": 12, "z": 7, "block_id": 2, "type": "flower", "age": 1 }
  ],
  "creatures": [
    { "id": 3, "type": "rat", "x": 10, "z": 44,
      "age": 2, "hunger": 3, "sleep": 0.5, "asleep": false,
      "needs": { "feed": 1, "sleep": 0.5 } }
  ],
  "drops": [
    { "id": 9, "item": "berry", "count": 2, "x": 5, "z": 5, "age": 3.2 }
  ]
}
```

### Top level

| Field | Type | Semantics |
|-------|------|-----------|
| `revision` | int | Monotonic counter, incremented once per simulation tick (`server.tick_rate` times/second, e.g. 20/s by default). Starts at `0` on server start. Use it as a cheap "has anything changed since I last polled" test — compare to the last value you applied. It resets to `0` if the server process is restarted (see §8). |
| `vegetation_revision` | int | Separate monotonic counter, incremented **only** when a flora block actually changes (grows a stage, dies, spawns, or is attacked/eaten down). Bumped roughly once per `cycle_length` seconds (or immediately on a flower being attacked). Use this to decide when to rebuild vegetation meshes/lists, independent of the much more frequent `revision`. Also resets to `0` on restart. |
| `chunk` | object | Static-for-the-session terrain metadata (see below) |
| `time` | object | Season/day/day-night-phase clock (see below) |
| `terrain` | object | Ground texture reference (see below) |
| `vegetation` | array | One entry per living flora block currently on the map (see below) |
| `creatures` | array | One entry per living creature instance (see below) |
| `drops` | array | One entry per un-expired, un-collected item drop on the ground (see below) |

### `chunk`

| Field | Type | Semantics |
|-------|------|-----------|
| `size` | `[int, int, int]` | World dimensions as `[x, y, z]` (JSON array; matches `Chunk.size`). The demo world generator always produces a cube, e.g. `[100, 100, 100]`, but don't assume `x == y == z`. |
| `surface_y` | int | The single Y-level (`size[1] - 1`) at which all terrain, vegetation, and creatures currently live — see §9 for the flatness assumption this implies. |

This object is effectively constant while the server is running (it only
changes if the world file itself is regenerated and the server restarted).

### `time`

| Field | Type | Semantics |
|-------|------|-----------|
| `season` | string | One of the keys under `seasons` in `config.json` — by default `"spring"`, `"summer"`, `"fall"`, `"winter"`, in that cycle order |
| `cycle` | int | Number of completed simulation cycles (`cycle_length` seconds each) since server start. Advances the season every `season_length` cycles. |
| `day` | int | Number of day/night transitions (sunrises) since server start; starts at `0`/`1` (see below) |
| `is_day` | bool | `true` while `phase < 40/60 (≈0.667)`, i.e. during the "day" portion of the cycle; `false` during "night" |
| `phase` | float | `0.0`–`1.0` fraction through the current `day_night_cycle`, computed as `(effective_time % day_night_cycle) / day_night_cycle` on the server, where `effective_time` is the real wall clock plus an accumulated virtual-time offset from `speed_multiplier` (see §4b). **Not** relative to server start — at the default multiplier of `1` the offset never leaves `0`, so `effective_time` is byte-for-byte `time.time()` and absolute value still has no cross-machine meaning, only its progression matters. At `speed_multiplier > 1`, `phase` (and everything else in §4b's list) progresses faster than real elapsed time by that factor. |
| `day_night_cycle` | float | Seconds for one full day+night loop (echo of `config.json`'s `day_night_cycle`, hot-reloadable) |

`day` increments the instant `is_day` flips `false → true` (dawn), and that
same transition triggers the server's daily lifecycle events (hunger/aging,
wake-up — see [`README.md`](./README.md#simulation-overview-server-authoritative)). `day` starts at
`0` and typically reads `1` within the first cycle since dawn happens almost
immediately after boot in most runs.

### `terrain`

| Field | Type | Semantics |
|-------|------|-----------|
| `texture` | string | Repo-relative texture path for the ground, e.g. `"textures/soil.png"`, `"textures/soil_fall.png"`, `"textures/soil_winter.png"`. Changes only at season boundaries. **This is a path string, not image bytes or a URL** — the API does not serve the file itself (see §9). |

### `vegetation[]` — one entry per flora block

```json
{ "x": 12, "z": 7, "block_id": 2, "type": "flower", "age": 1 }
```

| Field | Type | Semantics |
|-------|------|-----------|
| `x`, `z` | int | Block position on the surface layer (`y` is always `chunk.surface_y`) |
| `block_id` | int | Raw terrain block ID from [`chunk.py`](./chunk.py): `2` = flower, `3` = bush, `4` = tree, `5` = grass patch (`0`=air, `1`=the default bare-ground block, internally still named `GRASS` in `chunk.py` for save-format/historical reasons even though it now renders as soil — never appear here, only actual flora blocks are listed) |
| `type` | string | Vegetation definition name from `entities.json`'s `vegetation[]` (`"flower"`, `"bush"`, `"tree"`, `"grass"`) — the human-readable equivalent of `block_id`; look this up to get stage/texture metadata (see §9) |
| `age` | int \| `null` | Current vegetation age used to resolve the growth **stage** (see §9). **Nullable**: it is `null` only in the edge case where a flora block exists on the map but has no tracked age yet (e.g. a freshly loaded/foreign save missing that entry). Treat `null` the same as "unknown — use the definition's `initial_age`" the way [`main.py`](./main.py) does. |

There is **no** entry for empty/bare-ground/air tiles — the array only lists
occupied flora positions, so its length is proportional to how much
vegetation exists, not to world size.

### `creatures[]` — one entry per living creature instance

```json
{ "id": 3, "type": "rat", "x": 10, "z": 44,
  "age": 2, "hunger": 3, "sleep": 0.5, "asleep": false,
  "needs": { "feed": 1, "sleep": 0.5 } }
```

| Field | Type | Semantics |
|-------|------|-----------|
| `id` | int | **Stable, per-instance identifier**, assigned once at spawn/birth and never reused or renumbered. It is *not* an array index — safe to use as a dictionary/map key across polls to add/update/remove individual entities instead of rebuilding the whole list. IDs are monotonically increasing and **survive a server restart**: creatures are saved to `chunks/chunk_0_0.wrld` and restored with their ids intact (see §8). Don't rely on that for correctness though — a restart still resets `revision`, which is your signal to rebuild from scratch. |
| `type` | string | Creature definition name from [`entities.json`](./entities.json) (e.g. `"rat"`, `"rabbit"`) — look this up in `entities.json`'s `creatures[]` to get texture/behavior metadata (see §9) |
| `x`, `z` | int | Current tile position (`y` is always `chunk.surface_y + y_offset` from the creature's definition, entirely a client-side rendering concern) |
| `age` | int | Remaining lifespan in "age units"; decremented once per day once `hunger` is `0`, or once per winter start; creature is removed when `age <= 0` |
| `hunger` | int | Current hunger/satiation counter, `0`..`initial_hunger` (from `entities.json`); decreases food need, increases (up to the cap) when eating |
| `sleep` | float | Current sleep-need accumulator. **Not capped/thresholded** — it just competes against `feed` need for priority each move tick; resets to `0.0` at dawn or immediately on waking. Present (defaulting to `0.0`) even for creature types that don't define a `sleep` need. |
| `asleep` | bool | `true` while the creature has chosen to sleep (stops moving at night); always resets to `false` at dawn. Client should swap to the definition's `sleep_texture` while this is `true` (see §9). |
| `needs` | object | The server's **currently computed** need→priority map for this instance (same values `_creature_move` uses to decide behavior this tick), keyed by whichever needs are listed in the creature's `entities.json` definition (e.g. `{"feed": 1, "sleep": 0.5}`). `feed` is `max(0, initial_hunger - hunger)`; `sleep` mirrors the `sleep` field but reads as `0` while `asleep` is `true`. A creature with no configured needs reports `{}`. This is derived/informational — you don't need it to render the world, only to inspect *why* a creature is behaving a certain way (see the `/` inspector page, §4a). |

Creatures never appear/disappear mid-array-shuffle — entries are only added
(birth on each season change) or removed (death from starvation/old age/winter) between
snapshots; existing IDs' fields update in place.

### `drops[]` — one entry per item drop currently on the ground

```json
{ "id": 9, "item": "berry", "count": 2, "x": 5, "z": 5, "age": 3.2 }
```

| Field | Type | Semantics |
|-------|------|-----------|
| `id` | int | Stable per-instance identifier (same guarantees as `creatures[].id`), assigned at drop spawn time; resets on server restart |
| `item` | string | Item definition name from `entities.json`'s `items[]` (`"seed"`, `"berry"`, `"meat"`, `"log"`, `"stick"`) — look this up for the item's `texture`/`tags` |
| `count` | int | Number of stacked units at this position (a single drop entity can represent multiple units, e.g. `"count": 2` berries) |
| `x`, `z` | int | Ground tile position |
| `age` | float | **Seconds of effective (speed-multiplier-scaled) time since this drop spawned**, computed fresh on every `/state` call as `now - spawn_time` where both use the same `effective_time` as `time.phase` (see §4b) — this value keeps increasing between polls even though the underlying drop object doesn't otherwise change, and it is **not** wall-clock-continuous the way real elapsed time is if `speed_multiplier != 1` (see §10 for local extrapolation). Drops are removed once `age >= drop_lifetime` (from `config.json`) or once eaten by a creature. |

---

## 6. IDs and revisions — quick reference

| Concept | Resets when | Use it to… |
|---|---|---|
| `creatures[].id` | Never (persisted) | Track/diff individual creatures across polls |
| `drops[].id` | Server restart | Track/diff individual drops across polls |
| `revision` | Server restart | Detect *any* tick has happened / detect restart (goes backwards) |
| `vegetation_revision` | Server restart | Detect flora actually changed (rebuild vegetation meshes only when needed) |

What the world file (`chunks/chunk_0_0.wrld`) keeps across a restart:

- Terrain, vegetation, and their ages.
- **All live fauna** — each creature's tile, age, hunger, sleep state and
  `id`, plus the id counter, so a reloaded world neither reseeds its
  population nor reissues an id that's already in use.
- **The simulation clock** — `time.season`, `time.cycle` and `time.day` all
  resume where they left off, so a restart no longer snaps the world back to
  spring, day 0.

What it does *not* keep: item drops (their `age` is measured against
simulation time, so they're dropped rather than resurrected with a bogus
lifetime), `drops[].id`, both revision counters, and `speed_multiplier`. A
fresh server process therefore starts both revisions at `0` and reissues drop
IDs from near `1`.

`time.is_day` and `time.phase` are derived from the wall clock, not saved:
there is nothing meaningful to carry across a restart. The server does seed
its internal night→day edge detector from whichever phase the world is
actually in at startup, so `time.day` still advances at the first dawn after
a restart that happened during the night.

---

## 7. Snapshot consistency, thread safety & authoritative state

- `World.lock` (a `threading.RLock`) is held around **every** read or
  mutation that needs a consistent view: the simulation tick loop takes it
  once per tick, and each HTTP handler takes it for the duration of building
  its response. This means:
  - `GET /state` always returns an internally consistent snapshot — you will
    never see, e.g., a creature's `hunger` from tick *N* alongside
    `vegetation` from tick *N+1*. The whole snapshot is built under one lock
    acquisition.
  - The simulation tick and any HTTP request are mutually exclusive — a
    slow client reading `/state` briefly pauses the tick loop (and vice
    versa), but the lock section is short (pure in-memory dict/list
    construction, no I/O), so this is not observable as a performance issue
    in practice.
- The server is the **sole source of truth**. Clients must treat every
  field in `/state` as read-only, authoritative data — there is no
  request that lets a client push position/state changes back into the
  simulation (the only mutating endpoint is `POST /save`, which persists
  the *current* authoritative state to disk and does not accept any input).
- Because `/state` always returns the complete world, there is no partial
  update, delta, or "since revision X" query — every poll re-transmits
  everything. Clients are expected to diff two full snapshots themselves
  (by `revision`/`vegetation_revision`/entity `id`) rather than ask the
  server for a delta.

---

## 8. Coordinate system & `surface_y`

- The world is a 3D block grid sized `chunk.size = [x, y, z]`; blocks are
  addressed by integer `(x, y, z)`.
- **Every** entity currently reported by `/state` — terrain, vegetation, and
  creatures — lives on exactly one Y-level: `surface_y = size[1] - 1` (the
  top of the world). Nothing in the current implementation places anything
  at any other height, and the API does not expose a per-column heightmap.
- `(x, z)` in `vegetation[]`/`creatures[]`/`drops[]` therefore fully
  determines position; a client only needs `surface_y` once (from `chunk`)
  to know the constant Y to render everything at (plus each creature's own
  `y_offset` from `entities.json`, purely a rendering nicety).
- This is a known simplification the project intends to revisit if terrain
  height ever becomes non-flat — at that point `/state` would need to grow a
  heightmap or per-entity Y, which is **not** implemented today. Don't build
  client logic that assumes today's `surface_y`-for-everything constant will
  necessarily hold in a hypothetical future version — but it is safe to rely
  on for the API described in this document.

---

## 9. Resolving textures/stages from `entities.json`

**The server does not embed texture paths, stage thresholds, or item/creature
metadata into `/state`.** Only dynamic, per-instance numbers come from the
API (`block_id`, `age`, `type`, `item`, positions, counters). Static
definitions — textures, growth-stage thresholds, spawn rules, diets,
per-stage loot — live entirely in [`entities.json`](./entities.json), which **both**
the server (for simulation rules) and any client (for rendering) must load
independently. This keeps snapshots small and avoids re-sending unchanging
data on every poll.

**Implication for your client:** you must ship/read the *same*
`entities.json` the server is using (see the file-order/compatibility note
below) and re-derive display information locally, exactly as [`main.py`](./main.py)
does. The three lookups you need:

### Vegetation stage → texture

```python
def get_stage(vdef, age):
    for stage in vdef["stages"]:       # in file order, ascending max_age
        if age <= stage["max_age"]:
            return stage
    return vdef["stages"][-1]          # fallback: oldest/last stage
```

1. Look up `vdef = veg_defs_by_block_id[vegetation_entry["block_id"]]`.
2. If `vegetation_entry["age"]` is `null`, substitute `vdef["initial_age"]`.
3. Walk `vdef["stages"]` **in file order** and return the first stage whose
   `max_age` is `>= age`; the last stage is the catch-all for anything older
   (this also matches how the server itself picks a stage for loot via
   `_get_stage`).
4. Use that stage's `texture` (and `height`, if you're building 3D geometry)
   for rendering.

### Creature awake/asleep texture

```python
cdef = creature_defs_by_name[creature_entry["type"]]
texture = cdef["sleep_texture"] if creature_entry["asleep"] else cdef["texture"]
```

`sleep_texture` is only meaningful (and only guaranteed present) for
creature types whose `entities.json` definition actually includes it — check
before falling back, e.g. `cdef.get("sleep_texture", cdef["texture"])`, in
case a future creature type has `asleep` semantics but no distinct sprite
yet.

### Item drop texture

```python
idef = item_defs_by_name[drop_entry["item"]]
texture = idef["texture"]
```

Every current item definition (`seed`, `berry`, `meat`, `log`, `stick`)
includes a `texture`; a defensive client should still tolerate a missing
key (fall back to a placeholder/color) for forward compatibility.

### File-order / compatibility implications

- `entities.json`'s array order matters for two things beyond simple lookup:
  spawn priority (flora are tried **flower → bush → tree → grass**, first
  successful roll per tile wins) and stage resolution (stages are scanned
  in file order, so `stages[]` **must** stay sorted by ascending
  `max_age`). If you maintain your own copy or a variant of `entities.json`,
  preserve stage ordering or stage resolution will silently pick the wrong
  texture. A vegetation entry's `spawn.active_seasons` (optional array of
  season names) additionally gates the *whole entry* to only attempt its
  chance roll during those seasons — `grass` uses this to stop regrowing in
  fall/winter; entries without it are unrestricted.
- The server and any client **must agree on `entities.json` contents** —
  `block_id` values, item/creature names, and stage counts are the join
  keys between `/state`'s terse per-instance data and your local
  definitions. A client running an out-of-sync `entities.json` (e.g. a
  `block_id` remapped, or a creature `type` renamed) will render incorrect
  textures/stages even though the wire protocol itself is unaffected —
  this is a data-contract compatibility risk, not something the API
  can detect or guard against for you.
- Both processes read `entities.json` from disk independently at their own
  startup — there is no endpoint to fetch entity definitions or textures
  over HTTP; your client needs local filesystem access to the same repo
  contents (`entities.json` **and** the referenced files under `textures/`).

---

## 10. Polling, reconnect & synchronization recommendations

These mirror what [`main.py`](./main.py) actually implements; treat them as proven,
practical guidance rather than mere suggestions.

### Poll on a background thread

`GET /state` should be called on its own thread/timer, decoupled from
rendering/UI/input handling, so a slow or unreachable server never blocks
your main loop. Use a short request timeout (the bundled client defaults to
`client.request_timeout = 2.0` seconds from `config.json`) and swallow
connection errors — just mark yourself "disconnected" and keep retrying on
a fixed interval (`client.poll_interval = 0.15` seconds by default is a
reasonable starting point; tune to your rendering cadence and network).

### Detect server restart via `revision` going backwards

Because `revision` (and `vegetation_revision`, and every entity `id`) resets
to near-zero when the server process restarts, a simple and reliable
"fresh session" detector is:

```
if new_snapshot.revision < last_applied_revision:
    # server restarted — discard all local entities/state and rebuild
    # from scratch using the new snapshot as if it were the first one.
```

Don't rely on a specific reset value (e.g. exactly `0`) — just check for a
decrease relative to the last value you applied.

### Only rebuild what actually changed

- Skip reprocessing a snapshot entirely if `revision` is unchanged since
  your last poll (nothing has ticked).
- Only rebuild vegetation meshes/lists when `vegetation_revision` changes
  (it changes far less often than `revision` — roughly once per
  `cycle_length` seconds, versus every tick).
- Diff `creatures[]`/`drops[]` by `id` every time `revision` changes: keep a
  local map from `id → your-entity`, add new IDs, update existing ones
  in-place, and remove/destroy any local entity whose `id` is no longer
  present in the latest snapshot.

### Local phase interpolation (smooth day/night without polling faster)

`time.phase` is a snapshot of server wall-clock progress at the moment the
response was built — by the time you render it, some real time has already
elapsed. Extrapolate locally between polls instead of freezing visuals at
poll cadence:

```python
elapsed = time.time() - received_at        # received_at: your local clock when this snapshot arrived
phase = (snapshot.time.phase + elapsed / snapshot.time.day_night_cycle) % 1.0
is_day = phase < (40.0 / 60.0)
```

Use the interpolated `phase`/`is_day` for lighting/visual transitions every
frame; only trust the *raw* snapshot value for logic that must match the
server exactly (there isn't any such client-side logic in the current
implementation — day/night is purely cosmetic client-side).

> **Note on `speed_multiplier` (§4b):** this extrapolation assumes 1 second
> of client-side `time.time()` corresponds to 1 second of server progress.
> That's only true at the default `speed_multiplier = 1`. At a higher
> multiplier, the server's `phase` actually advances faster than this local
> extrapolation assumes, so visuals will slightly under-shoot between polls
> and "catch up" (a small, self-correcting jump) on the next one. This is
> harmless with the default `poll_interval` (0.15s) relative to typical
> `day_night_cycle` values, but if you poll much less frequently while also
> running at a high multiplier, consider also reading `/admin` and scaling
> the extrapolated `elapsed` by the current `speed_multiplier`.

### Local drop "bobbing" animation

`drops[].age` is likewise a point-in-time snapshot value. For a subtle
floating/bobbing effect without needing faster polling:

```python
age = drop_age_from_last_snapshot + (time.time() - received_at)
# Billboard quads are centered on their position — place the center high
# enough that the whole icon clears the visual ground (surface_y+0.5) and
# any grass surface decal (surface_y+0.51).
drop_scale = 0.5
hover = 0.2
y = surface_y + 0.5 + (drop_scale / 2) + hover + 0.08 * sin(age * 2.0)
```

Recommended starting constants (from `main.py`): scale `0.5`, hover `0.2`
above the surface, bob amplitude `0.08`, speed `2.0`. A base of
`surface_y + 0.52` with a centered 0.4-tall quad buries half the icon
under the ground — avoid that. Give each drop a small random per-instance
`(x, z)` jitter the first time you see its `id` (purely cosmetic, so
newly-adjacent drops don't render perfectly overlapping) and keep reusing
that same jitter for the drop's lifetime — don't re-roll it every poll.

---

## 11. Client examples

All examples assume a server already running with defaults
(`http://127.0.0.1:8765`).

### curl

```sh
# Liveness check
curl http://127.0.0.1:8765/health

# Full snapshot
curl http://127.0.0.1:8765/state

# Force a save (no body needed/accepted)
curl -X POST http://127.0.0.1:8765/save

# Unknown route -> 404 JSON error
curl -i http://127.0.0.1:8765/nope
```

### PowerShell

```powershell
# Liveness check
Invoke-RestMethod -Uri http://127.0.0.1:8765/health

# Full snapshot, then inspect creatures
$state = Invoke-RestMethod -Uri http://127.0.0.1:8765/state
$state.creatures | Format-Table id, type, x, z, hunger, asleep

# Force a save
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/save

# Simple poll loop, printing revision whenever it changes
$lastRevision = -1
while ($true) {
    $state = Invoke-RestMethod -Uri http://127.0.0.1:8765/state
    if ($state.revision -ne $lastRevision) {
        Write-Host "revision=$($state.revision) veg_rev=$($state.vegetation_revision) creatures=$($state.creatures.Count) drops=$($state.drops.Count)"
        $lastRevision = $state.revision
    }
    Start-Sleep -Milliseconds 150
}
```

### Python (stdlib only — `urllib`, no external packages)

```python
"""Minimal MidTerraSim client using only the Python standard library."""
import json
import time
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8765"


def get_json(path, method="GET", timeout=2.0):
    req = urllib.request.Request(BASE_URL + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_loop():
    last_revision = None
    last_veg_revision = None
    known_creature_ids = set()
    known_drop_ids = set()

    while True:
        try:
            snap = get_json("/state")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
            print(f"disconnected: {exc}; retrying…")
            time.sleep(1.0)
            continue

        # Detect a server restart: revision moved backwards.
        if last_revision is not None and snap["revision"] < last_revision:
            print("server restarted — resetting local state")
            known_creature_ids.clear()
            known_drop_ids.clear()
            last_veg_revision = None

        if snap["revision"] != last_revision:
            creature_ids = {c["id"] for c in snap["creatures"]}
            drop_ids = {d["id"] for d in snap["drops"]}
            for new_id in creature_ids - known_creature_ids:
                print(f"creature spawned: {new_id}")
            for gone_id in known_creature_ids - creature_ids:
                print(f"creature removed: {gone_id}")
            for new_id in drop_ids - known_drop_ids:
                print(f"drop spawned: {new_id}")
            for gone_id in known_drop_ids - drop_ids:
                print(f"drop collected/expired: {gone_id}")
            known_creature_ids, known_drop_ids = creature_ids, drop_ids
            last_revision = snap["revision"]

        if snap["vegetation_revision"] != last_veg_revision:
            print(f"vegetation changed: {len(snap['vegetation'])} flora blocks")
            last_veg_revision = snap["vegetation_revision"]

        time.sleep(0.15)


if __name__ == "__main__":
    print(get_json("/health"))
    poll_loop()
```

---

## 12. Compatibility & evolution guidance

- **Tolerate unknown fields.** Future server versions may add new top-level
  or nested fields (e.g. new `time` fields, new per-creature stats). Parse
  JSON generically and ignore anything you don't recognize rather than
  failing on unexpected keys.
- **Treat omitted/optional fields safely.** `vegetation[].age` is the one
  documented nullable field today; a robust client should generally code
  defensively for any field that this document marks nullable or "may be
  absent for some definitions" (e.g. a creature type without
  `sleep_texture`) by falling back to a sane default instead of raising.
- **The API is unauthenticated and localhost-only by default.** There is no
  API key, token, TLS, or origin check of any kind — anyone who can reach
  the bound host/port can read the full world state and force a save.
  This is fine for the default `127.0.0.1` binding (only local processes can
  connect).
  > ⚠️ **Security warning:** binding with `--host 0.0.0.0` (or any
  > non-loopback address) exposes this **unauthenticated** API to your
  > entire network (or the internet, if port-forwarded/firewalled through).
  > Anyone on that network can then read your full world state and trigger
  > saves. Only do this on trusted networks, and prefer adding your own
  > reverse proxy / firewall rule / VPN in front of it if remote access is
  > genuinely needed — the server itself implements no access control.
- **Don't rely on ID/revision values being small or starting at exactly
  zero/one** — only rely on the *ordering* and *reset-on-restart*
  guarantees described in §6.
- **Don't assume connection reuse.** Given the `HTTP/1.0`, connection-per-
  request behavior (§2), avoid client configurations that assume
  keep-alive/pipelining against this server.

## 13. Limitations

- **Read-only world snapshot, with one exception.** `GET /state` and
  `GET /health` are pure reads; the *only* mutating call the API exposes at
  all is `POST /save` (force a disk write of current state) — there is no
  way to move a creature, place/remove vegetation, spawn/collect a drop,
  or otherwise influence the simulation via this API.
- **No streaming/WebSocket/push channel.** This is a plain request/response
  HTTP API; every update must be discovered by polling `GET /state` (or
  `GET /health` for a cheap revision check) — there is no subscribe/notify
  mechanism, long-polling, Server-Sent Events, or WebSocket upgrade
  implemented.
- **Polling model only**, and every poll returns the *entire* current
  world — there is no delta/"changes since revision X" query, so
  bandwidth/parse cost scales with total world size (vegetation count +
  creature count + drop count) on every single call, not with how much
  actually changed.
- **The UI client's player/camera state is entirely local and not part of
  this API.** Player position, look direction, sprint/jump state, and any
  other first-person-controller state in `main.py` are purely
  client-side rendering concerns — they are never sent to the server and
  never appear anywhere in `/state`. Two independent clients polling the
  same server would each have their own private, unsynchronized player
  position; the API has no concept of "players" at all.
