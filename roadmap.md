# MidTerraSim -- Roadmap

Planned work, not yet implemented. This is a living backlog -- items get
added, reprioritized, split, or dropped as the project evolves. See
[README.md](./README.md) for what's already built and
[takeover.md](./takeover.md) for session-by-session history of what's shipped.

Status legend: unstarted / in-progress / done (moved to README/takeover)

---

## Bugs

- [x] **Fix drop display** -- drops were centered at `surface_y + 0.52` with
  a 0.4-tall billboard, so half the icon sat under the ground/grass.
  Client now places the billboard center at
  `surface_y + 0.5 + scale/2 + hover` (`DROP_SCALE=0.5`, `DROP_HOVER=0.2`)
  so the full icon floats above the surface.

## Rendering & Terrain

- [x] **Redo grass from cross-billboard to surface decal** -- grass patches
  render as a flat horizontal quad flush with the terrain top
  (`stage.render: "surface"` + `textures/grass.png`), covering the soil
  tile instead of a short vertical cross-billboard. Implemented in
  `build_surface_veg_mesh` / `_rebuild_vegetation` in `main.py`.
- [ ] **Switch from flat to generated surface** -- terrain is currently a
  single flat plane at a constant `surface_y` for the whole chunk (both
  `chunk.py`'s data model and `main.py`'s `build_ground_mesh` assume this).
  Introduce actual height variation (hills, slopes, valleys) via procedural
  generation (e.g. Perlin/simplex noise in `generate_chunk.py`). This is a
  substantial change -- the server's `/state` schema documents the
  flat-surface assumption explicitly (see `SERVER_CLIENT_API.md` Sec 9), so
  the API would need a heightmap or per-column surface data, and the
  client's ground mesh builder would need real per-vertex heights instead
  of a constant `sy`.
- [ ] **Add water bodies** -- lakes/rivers/ponds, presumably tied to the
  generated (non-flat) surface above. Needs a water block/tile type, a
  render approach (transparent/animated water shader or simple tinted
  plane), and likely interacts with fish (below) and possibly
  creature/crop placement rules (e.g. crops or thirsty animals near water).

## Fauna (new creatures)

All of these plug into the existing generic creature system in
`entities.json` (`needs`, `diet`, movement, reproduction, drops) -- see
`server.py`'s `_creature_move`/`_compute_creature_needs`/`_act_feed` and
`takeover.md`'s "Creature needs / feed + sleep AI" section for the pattern
established by rats.

- [x] **Rabbits** -- herbivore; eats grass cover then browses bushes;
  same needs/sleep/movement pattern as rats (`entities.json` + plant diet
  feed AI).
- [ ] **Foxes** -- predator; would need a new "hunt" need/behavior
  (attacking rabbits/rats rather than flowers), since the current feed AI
  only attacks flowers. First carnivore in the ecosystem.
- [ ] **Wolves** -- predator, larger/pack-oriented; may want a "pack" or
  "group" mechanic distinct from foxes, or could reuse the same predator
  behavior at a different scale/threat level.
- [ ] **Birds** -- likely airborne (first creature not confined to
  `surface_y`), decorative or minor ecosystem role (eating seeds?). Needs
  the creature movement/rendering system to support a flying Y-offset/motion
  pattern instead of grid-locked ground movement.
- [ ] **Fish** -- depends on water bodies existing first; confined to water
  tiles, simplest possible movement (or purely decorative/catchable).
- [ ] **Humans** -- most complex addition; likely needs its own behavior
  tree entirely distinct from animal needs/diet (tools, farming
  interaction, building?, dialogue?). Scope this out separately once the
  animal ecosystem (predator/prey, water, crops) is in place -- humans
  probably depend on several of the above being done first (e.g. crops to
  farm, water to drink, animals to hunt/herd).

## Flora variety

Currently flower/bush/tree are each a single generic type. Extend
`entities.json`'s `vegetation[]` with multiple distinct entries per category
(own textures, stage tables, spawn rules) instead of one-size-fits-all:

- [ ] **Different flowers** instead of one generic flower (e.g. daisy,
  tulip, rose -- varying color/size/rarity).
- [ ] **Different bushes** instead of one generic bush (e.g. berry bush vs.
  thorn bush vs. flowering shrub).
- [ ] **Different trees** instead of one generic tree (e.g. oak, pine,
  birch -- varying height/canopy shape/season behavior, e.g. conifers not
  losing "leaves" in winter).

## Crops

- [ ] **Carrot, cabbage, wheat** -- new plantable/harvestable food crops,
  likely distinct from wild flora (player- or human-planted rather than
  randomly spawned?), feeding into the humans feature above and possibly
  usable as creature food too. Needs new item definitions (harvested crop
  -> food item) and probably new block/vegetation types with their own
  grow-stage textures.

---

## Suggested rough ordering

Not a commitment, just a reasonable dependency-aware sequence based on what
blocks what:

1. Flora variety (flowers/bushes/trees) -- extends existing systems, no new
   mechanics needed
2. Birds (simple new fauna, reuse the existing herbivore/feed patterns that
   rabbits now establish)
3. Generated surface (bigger change, needed before water bodies make sense)
4. Water bodies -> fish
5. Foxes, wolves (needs a new predator/hunt mechanic)
6. Crops (carrot, cabbage, wheat)
7. Humans (largest scope, benefits from crops/water/animals already
   existing)
