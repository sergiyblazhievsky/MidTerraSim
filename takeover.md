# MidTerraSim — Session Takeover

Handoff document for continuing work in a new chat. Last updated after commit on 2026-09-02.

---

## Project Summary

**MidTerraSim** is a first-person 3D ecosystem simulation (Python + [Ursina Engine](https://www.ursinaengine.org/)). The player walks a 100×100 procedural world while a background cellular-automaton simulation runs plant growth, seasons, day/night, and creature ecology.

```
python main.py           # play
python generate_chunk.py # reset world + regenerate base textures
python map_viewer.py     # top-down .wrld inspector (Tkinter)
```

Requirements: `pip install ursina pillow`

---

## Current Architecture

### Data files

| File | Role |
|------|------|
| `config.json` | Timing, seasons, `drop_lifetime` — hot-reloaded every frame |
| `entities.json` | Items, vegetation, creatures — loaded at startup |
| `chunks/chunk_0_0.wrld` | Saved world (gitignored) |

### `entities.json` structure

**Items** — seed, berry, meat tagged `raw` + `food`

**Vegetation** — flower, bush, tree with stages, spawn rules, per-stage loot

**Creatures** — rat with `needs: ["feed"]`, `diet: ["food"]`

Block IDs: `AIR=0`, `GRASS=1`, `FLOWER=2`, `BUSH=3`, `TREE=4`

### World drops

- Floating billboard quads with 16×16 textures (`textures/*_16.png`)
- Spawned on vegetation/creature death via `_drop_from()` (stage-aware)
- Expire after `drop_lifetime` seconds; no auto-pickup
- Eating handled only by creature feed AI

### Creature needs / feed AI

Each movement tick per fauna instance:

1. `_compute_creature_needs()` — `feed` = `initial_hunger - hunger`
2. `_pick_highest_need()` — highest value task, or none if all ≤ 0
3. `_creature_move()` — execute task or random walk

**Feed priority:**
1. Eat food on same tile (`_resolve_diet` → items with matching tags/names)
2. Attack flower on same tile
3. Step toward nearest food drop (5 block radius)
4. Step toward nearest dead flower, then live flower
5. Random move

Diet `"food"` resolves to seed, berry, meat via item tags.

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

## Key Code Locations (`main.py`)

| Area | Functions |
|------|-----------|
| Entity loading | `load_entities()`, `_veg_with_tag()`, `_items_with_tag()`, `_resolve_diet()` |
| World drops | `_spawn_drop()`, `_drop_from()`, `_update_drops()`, `ITEM_TEXTURES` |
| Simulation | `_sim_step()`, `_count_kind_near()` |
| Creature AI | `_compute_creature_needs()`, `_act_feed()`, `_creature_move()`, `_eat_food_at_block()` |
| Lifecycle | `_on_day_start()`, `_on_season_start()`, `_spawn_creature_at()`, `_remove_creature()` |

---

## Texture Assets

- **16×16** — ground tiles, drop icons (`*_16.png`)
- **64×64 cross** — vegetation billboards (`*_xcross_64.png`)
- `generate_chunk.py` also generates legacy `textures/seed.png`; runtime drops use `*_16.png`

---

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

- Extend `needs` beyond `feed` (thirst, shelter, etc.)
- Generalize feed attack beyond flowers (bushes? trees?)
- Align `generate_chunk.py` initial spawn order with runtime (flower first vs last)
- `map_viewer.py` — show bushes/trees, not just flowers
- Add `log`/`stick` item definitions with tags if needed for crafting later

---

## Quick Reference: Rat Config

```json
{
  "name": "rat",
  "needs": ["feed"],
  "diet": ["food"],
  "initial_hunger": 3,
  "attack": 1,
  "avoids_block_tag": "tree",
  "reproduce_count": [1, 6]
}
```
