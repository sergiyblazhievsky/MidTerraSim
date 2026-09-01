# MidTerraSim

A first-person 3D ecosystem simulation built with [Ursina Engine](https://www.ursinaengine.org/).

## Features

- **Procedural terrain** — 100×100 chunk with grass, flowers, bushes, and trees
- **Seasonal cycles** — Spring, Summer, Fall, Winter with different moisture and fertility values
- **Plant lifecycle** — age-based growth and death; dry/dead texture variants for aging plants
- **Day/Night cycle** — 40-second day, 20-second night with ambient lighting transitions
- **Creatures** — rats that roam during the day and rest at night
- **HUD** — compact top-left overlay showing current season, cycle, and time of day
- **External config** — all simulation parameters live in `config.json`, editable without rebuild

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

World state is saved to `chunks/chunk_0_0.wrld` on exit. To regenerate a fresh world:

```
python generate_chunk.py
```

## Configuration

Edit `config.json` to change simulation parameters at runtime (no rebuild needed):

```json
{
  "cycle_length": 300.0,
  "season_length": 10,
  "day_night_cycle": 60.0,
  "seasons": {
    "spring": { "moisture": 40, "fertility": 20, "texture": "grass.png" },
    "summer": { "moisture": 20, "fertility": 30, "texture": "grass.png" },
    "fall":   { "moisture": 30, "fertility": 40, "texture": "grass_fall.png" },
    "winter": { "moisture": 30, "fertility": 10, "texture": "grass_winter.png" }
  }
}
```

## Project Structure

```
MidTerraSim/
├── main.py            # Game loop, rendering, simulation logic
├── chunk.py           # World data model and persistence
├── generate_chunk.py  # Procedural world and texture generation
├── config.json        # Runtime simulation config
├── chunks/            # Saved world state
└── textures/          # All 64×64 PNG assets
```
