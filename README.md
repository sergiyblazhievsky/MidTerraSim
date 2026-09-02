# MidTerraSim

A first-person 3D ecosystem simulation built with [Ursina Engine](https://www.ursinaengine.org/). Walk a 100×100 procedural world while a cellular-automaton simulation drives plant growth, seasonal change, and creature behavior in the background.

## Features

- **Procedural terrain** — 100×100×100 chunk with grass, flowers, bushes, and trees
- **Seasonal cycles** — Spring, Summer, Fall, Winter with different moisture and fertility values
- **Plant lifecycle** — age-based growth and death; dry/dead texture variants for aging plants
- **Day/Night cycle** — 40-second day, 20-second night with ambient lighting transitions
- **Data-driven entities** — vegetation, items, and creatures defined in `entities.json`
- **World drops** — dead plants and creatures leave floating item drops with 16×16 icons
- **Creature needs AI** — rats evaluate hunger-driven `feed` tasks before moving randomly
- **HUD** — compact top-left overlay showing current season, cycle, day count, and time of day
- **External config** — simulation timing and season parameters live in `config.json`, editable at runtime
- **Map viewer** — top-down Tkinter tool for inspecting saved `.wrld` files

## Controls

| Key | Action |
|-----|--------|
| WASD | Move |
| Mouse | Look |
| Shift | Sprint |
| Space | Jump |
| Esc | Save & quit |

## Requirements

```
pip install ursina pillow
```

## Running

```
python main.py
```

World state is saved to `chunks/chunk_0_0.wrld` on exit. To regenerate a fresh world with procedural textures:

```
python generate_chunk.py
```

To inspect a saved world from above:

```
python map_viewer.py
```

## Configuration

### `config.json`

Edit at runtime (no rebuild needed):

```json
{
  "cycle_length": 300.0,
  "season_length": 10,
  "day_night_cycle": 60.0,
  "drop_lifetime": 60.0,
  "seasons": {
    "spring": { "moisture": 40, "fertility": 20, "texture": "grass.png" },
    "summer": { "moisture": 20, "fertility": 30, "texture": "grass.png" },
    "fall":   { "moisture": 30, "fertility": 40, "texture": "grass_fall.png" },
    "winter": { "moisture": 30, "fertility": 10, "texture": "grass_winter.png" }
  }
}
```

| Key | Description |
|-----|-------------|
| `cycle_length` | Seconds between simulation ticks |
| `season_length` | Simulation cycles per season |
| `day_night_cycle` | Seconds for one full day/night loop |
| `drop_lifetime` | Seconds before uncollected item drops expire |

### `entities.json`

Defines items, vegetation, and creatures.

**Items** — `name`, `tags` (e.g. `raw`, `food` for seed/berry/meat)

**Vegetation** — `tags`, `block_id`, `stages[]` (texture, height, per-stage loot), `spawn` rules

**Creatures** — `needs`, `diet` (item names or tags), hunger/age, movement, reproduction, death loot

Block IDs (`chunk.py`): `AIR=0`, `GRASS=1`, `FLOWER=2`, `BUSH=3`, `TREE=4`

#### Spawn priority (runtime)

Flora are tried in file order on each eligible grass tile: **flower → bush → tree**. First successful roll wins.

| Plant | Chance | Constraints |
|-------|--------|-------------|
| Flower | 9% | Max 1 neighbor flower within radius 1 |
| Bush | 3% | No tree/bush within radius 1 |
| Tree | 1% | No tree within radius 2, no bush within radius 1 |

All spawns also require a fertility roll (`random 0–100 ≤ chunk.fertility`).

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
| Rat | — | 1 meat |

## Simulation Overview

Each simulation cycle:

1. Season may advance, updating moisture, fertility, and ground texture
2. Flora ages and may die when moisture is low; dead plants spawn stage-based item drops
3. New flora may spawn on empty grass tiles (fertility roll + per-type chance + proximity rules)
4. Fauna creatures act on needs-driven tasks or move randomly

### Creature feed AI (rats)

When hungry (`feed` need = `initial_hunger - hunger`):

1. Eat food drops on same tile (seed, berry, meat — resolved via `food` tag)
2. Attack flower on same tile (reduce age by `attack`)
3. Move toward nearest food drop within 5 blocks
4. Move toward nearest dead flower, then live flower
5. Random move if nothing found

When full: random walk only (avoids trees).

Daily and seasonal events:

- **Day start** — fauna lose 1 hunger (or 1 age if starving)
- **Summer start** — fauna reproduce near existing individuals
- **Winter start** — all fauna lose 1 age

## Project Structure

```
MidTerraSim/
├── main.py            # Game loop, rendering, simulation logic
├── chunk.py           # World data model and persistence
├── generate_chunk.py  # Procedural world and texture generation
├── map_viewer.py      # Top-down .wrld file inspector (Tkinter)
├── config.json        # Runtime simulation timing and seasons
├── entities.json      # Items, vegetation, and creature definitions
├── takeover.md        # Session handoff notes for continuing development
├── chunks/            # Saved world state (.wrld)
└── textures/          # PNG assets (16×16 and 64×64 variants)
```
