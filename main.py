"""
MidTerraSim — main entry point.
Loads the chunk from disk, renders terrain + vegetation, runs a cellular-automaton
simulation on a timed cycle, and saves the world on exit.

Controls: WASD to move, mouse to look, Shift to sprint, Space to jump, Esc to quit.
"""

import json
import random
from math import pi, sin
from pathlib import Path
from ursina import *
from ursina.prefabs.first_person_controller import FirstPersonController
from chunk import Chunk, AIR, GRASS

WORLD_FILE = 'chunks/chunk_0_0.wrld'
CONFIG_PATH = Path(__file__).with_name('config.json')
ENTITIES_PATH = Path(__file__).with_name('entities.json')

DEFAULT_CONFIG = {
    'cycle_length': 300.0,
    'season_length': 10,
    'day_night_cycle': 60.0,
    'drop_lifetime': 60.0,
    'seasons': {
        'spring': {'moisture': 40, 'fertility': 20, 'texture': 'grass.png'},
        'summer': {'moisture': 20, 'fertility': 30, 'texture': 'grass.png'},
        'fall': {'moisture': 30, 'fertility': 40, 'texture': 'grass_fall.png'},
        'winter': {'moisture': 30, 'fertility': 10, 'texture': 'grass_winter.png'},
    },
}


def load_config(path=CONFIG_PATH):
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding='utf-8')

    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)

    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(data)
    for season_name, default_data in DEFAULT_CONFIG['seasons'].items():
        merged['seasons'].setdefault(season_name, {})
        merged['seasons'][season_name] = {**default_data, **merged['seasons'][season_name]}
    return merged


config = load_config()
cycle_length = float(config['cycle_length'])
season_length = int(config['season_length'])
day_night_cycle = float(config['day_night_cycle'])
drop_lifetime = float(config['drop_lifetime'])
seasons = config['seasons']
SIM_INTERVAL = cycle_length

# ── entity definitions ────────────────────────────────────────────────────────
def load_entities(path=ENTITIES_PATH):
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)

entities_cfg = load_entities()

# Build lookup: block_id -> vegetation definition
veg_defs = {v['block_id']: v for v in entities_cfg.get('vegetation', [])}
VEGETATION_BLOCK_IDS = set(veg_defs.keys())
item_defs = {i['name']: i for i in entities_cfg.get('items', [])}
flower_vdef = next((v for v in entities_cfg.get('vegetation', []) if v.get('name') == 'flower'), None)

def _has_tag(edef, tag):
    return tag in edef.get('tags', [])

def _veg_with_tag(tag):
    """Return list of vegetation defs that have the given tag."""
    return [v for v in entities_cfg.get('vegetation', []) if _has_tag(v, tag)]

def _creatures_with_tag(tag):
    """Return list of (creature_index, creature_def) pairs that have the given tag."""
    return [(i, c) for i, c in enumerate(creature_defs) if _has_tag(c, tag)]

def _items_with_tag(tag):
    """Return item names that have the given tag."""
    return [name for name, idef in item_defs.items() if tag in idef.get('tags', [])]

def _resolve_diet(cdef):
    """Return set of edible item names from diet entries (item names or item tags)."""
    edible = set()
    for entry in cdef.get('diet', []):
        if entry in item_defs:
            edible.add(entry)
        else:
            edible.update(_items_with_tag(entry))
    return edible

def _resolve_avoids(cdef):
    """Return a set of block_ids this creature avoids, resolved from avoids_block_tag."""
    tag = cdef.get('avoids_block_tag')
    if tag:
        return {v['block_id'] for v in _veg_with_tag(tag)}
    bid = cdef.get('avoids_block')
    return {bid} if bid is not None else set()

# ── app setup ─────────────────────────────────────────────────────────────────
app = Ursina(title='MidTerraSim', borderless=False, fullscreen=False, development_mode=False, icon='textures/ursina.ico')
window.fps_counter.enabled = True

# ── load chunk ────────────────────────────────────────────────────────────────
chunk = Chunk.load(WORLD_FILE)
sx, sy, sz = chunk.size
SY = sy - 1   # surface y-level (99 for a full chunk)

# ── mesh helpers ──────────────────────────────────────────────────────────────
def _add_top_quad(vl, tl, ul, x, y, z):
    base = len(vl)
    vl += [(x-0.5, y+0.5, z-0.5), (x+0.5, y+0.5, z-0.5),
           (x+0.5, y+0.5, z+0.5), (x-0.5, y+0.5, z+0.5)]
    tl += [base, base+1, base+2, base, base+2, base+3]
    ul += [(0,0),(1,0),(1,1),(0,1)]

def _add_flower_cross(vl, tl, ul, x, y, z):
    for (dx0, dz0), (dx1, dz1) in [
        ((-0.5, -0.5), (+0.5, +0.5)),
        ((+0.5, -0.5), (-0.5, +0.5)),
    ]:
        base = len(vl)
        vl += [
            (x+dx0, y+0.5, z+dz0),
            (x+dx1, y+0.5, z+dz1),
            (x+dx1, y+1.5, z+dz1),
            (x+dx0, y+1.5, z+dz0),
        ]
        tl += [base,   base+1, base+2, base,   base+2, base+3]   # front
        tl += [base+2, base+1, base,   base+3, base+2, base  ]   # back
        ul += [(0,0),(1,0),(1,1),(0,1)]

def build_ground_mesh():
    gv, gt, gu = [], [], []
    for x in range(sx):
        for z in range(sz):
            for y in range(sy - 1, -1, -1):
                if chunk.get_block(x, y, z) != AIR:
                    _add_top_quad(gv, gt, gu, x, y, z)
                    break
    return Mesh(vertices=gv, triangles=gt, uvs=gu, mode='triangle')


def _get_stage(vdef, age):
    """Return the stage dict for this age (first stage whose max_age >= age)."""
    for stage in vdef['stages']:
        if age <= stage['max_age']:
            return stage
    return vdef['stages'][-1]


def build_veg_mesh(block_id, stage_index):
    """Build a cross mesh for all blocks of block_id whose stage matches stage_index."""
    vdef = veg_defs[block_id]
    verts, tris, uvs = [], [], []
    default_age = vdef['initial_age']
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) != block_id:
                continue
            age = chunk.vegetation_ages.get((x, z), default_age)
            stage = _get_stage(vdef, age)
            if vdef['stages'].index(stage) != stage_index:
                continue
            h = stage['height']
            for (dx0, dz0), (dx1, dz1) in [
                ((-0.5, -0.5), (+0.5, +0.5)),
                ((+0.5, -0.5), (-0.5, +0.5)),
            ]:
                base = len(verts)
                verts += [
                    (x+dx0, SY+0.5,   z+dz0),
                    (x+dx1, SY+0.5,   z+dz1),
                    (x+dx1, SY+0.5+h, z+dz1),
                    (x+dx0, SY+0.5+h, z+dz0),
                ]
                tris += [base, base+1, base+2, base, base+2, base+3]
                tris += [base+2, base+1, base, base+3, base+2, base]
                uvs += [(0,0),(1,0),(1,1),(0,1)]
    return Mesh(vertices=verts, triangles=tris, uvs=uvs, mode='triangle')

# ── build initial meshes ──────────────────────────────────────────────────────
print('Building meshes …')
terrain = Entity(
    model=build_ground_mesh(),
    texture=load_texture('textures/grass.png'),
    collider=None,
)

# one Ursina Entity per (block_id, stage_index) pair
veg_entities = {}   # (block_id, stage_index) -> Entity
for vdef in entities_cfg.get('vegetation', []):
    bid = vdef['block_id']
    for si, stage in enumerate(vdef['stages']):
        ent = Entity(
            model=build_veg_mesh(bid, si),
            texture=load_texture(stage['texture']),
            collider=None,
        )
        ent.setTransparency(1)
        veg_entities[(bid, si)] = ent

print('Meshes ready.')

# ── invisible floor collider ──────────────────────────────────────────────────
floor = Entity(
    model='cube',
    scale=(sx, 1, sz),
    position=(sx / 2 - 0.5, SY + 0.5, sz / 2 - 0.5),
    collider='box',
    visible=False,
)

# ── player ────────────────────────────────────────────────────────────────────
player = FirstPersonController(position=(sx / 2, SY + 2, sz / 2))
mouse.visible = False
mouse.locked = True

# ── creatures ─────────────────────────────────────────────────────────────────
# Lists of (positions, entities, creature_def) per creature type
all_creature_positions = []   # list of lists
all_creature_entities  = []   # list of lists
creature_defs          = []   # parallel list of creature config dicts

for cdef in entities_cfg.get('creatures', []):
    positions = []
    cent_list = []
    min_dist = cdef.get('min_spawn_distance', 2)
    avoids = _resolve_avoids(cdef)
    count = cdef.get('count', 1)
    sx_c, sz_c = cdef.get('scale', [1.0, 1.0])
    y_off = cdef.get('y_offset', 1.0)

    for _ in range(count):
        for _ in range(200):
            x = random.randint(0, sx - 1)
            z = random.randint(0, sz - 1)
            block_ok = chunk.get_block(x, SY, z) not in avoids
            spaced = all(abs(x - rx) + abs(z - rz) >= min_dist for rx, rz in positions)
            if chunk.get_block(x, SY, z) == GRASS and block_ok and spaced:
                positions.append((x, z))
                break
        else:
            positions.append((random.randint(0, sx - 1), random.randint(0, sz - 1)))

    for x, z in positions:
        ent = Entity(
            model='quad',
            texture=load_texture(cdef['texture']),
            position=(x, SY + y_off, z),
            scale=(sx_c, sz_c),
            billboard=True,
            double_sided=True,
            collider=None,
        )
        ent.setTransparency(1)
        cent_list.append(ent)

    all_creature_positions.append(positions)
    all_creature_entities.append(cent_list)
    creature_defs.append(cdef)

# per-instance state: list of lists of dicts {age, hunger, attack}
all_creature_stats = []

for ci, cdef in enumerate(creature_defs):
    init_age    = cdef.get('initial_age', 1)
    init_hunger = cdef.get('initial_hunger', 3)
    attack      = cdef.get('attack', 0)
    stats = [{'age': init_age, 'hunger': init_hunger, 'attack': attack}
             for _ in all_creature_positions[ci]]
    all_creature_stats.append(stats)

# ── world drops ───────────────────────────────────────────────────────────────
# Each drop: {'item', 'count', 'x', 'z', 'entity', 'spawn_time'}
world_drops = []

ITEM_TEXTURES = {
    'seed':  'textures/seed_16.png',
    'berry': 'textures/berry_16.png',
    'log':   'textures/log_16.png',
    'stick': 'textures/stick_16.png',
    'meat':  'textures/meat_16.png',
}
ITEM_COLORS = {
    'seed':  color.yellow,
    'berry': color.violet,
    'log':   color.rgb(0.4, 0.25, 0.1),
    'stick': color.rgb(0.6, 0.4, 0.2),
    'meat':  color.red,
}


def _spawn_drop(item, count, x, z):
    tex_path = ITEM_TEXTURES.get(item)
    if tex_path:
        ent = Entity(
            model='quad',
            texture=load_texture(tex_path),
            position=(x + random.uniform(-0.3, 0.3), SY + 0.52, z + random.uniform(-0.3, 0.3)),
            scale=(0.4, 0.4),
            billboard=True,
            double_sided=True,
            collider=None,
        )
        ent.setTransparency(1)
    else:
        col = ITEM_COLORS.get(item, color.white)
        ent = Entity(
            model='quad',
            color=col,
            position=(x + random.uniform(-0.3, 0.3), SY + 0.52, z + random.uniform(-0.3, 0.3)),
            scale=(0.35, 0.35),
            billboard=True,
            collider=None,
        )
    world_drops.append({
        'item': item, 'count': count, 'x': x, 'z': z,
        'entity': ent, 'spawn_time': time.time(), 'base_y': SY + 0.52,
    })
    print(f'[drop] {count}x {item} at ({x},{z})')


def _drop_from(edef, x, z, age=None):
    """Spawn drops from an entity def, using stage-level contains if available."""
    contains = None
    if age is not None and 'stages' in edef:
        stage = _get_stage(edef, age)
        contains = stage.get('contains')
    if not contains:
        contains = edef.get('contains', [])
    for entry in contains:
        lo, hi = entry['count'][0], entry['count'][1]
        count = random.randint(lo, hi)
        if count > 0:
            _spawn_drop(entry['item'], count, x, z)


def _update_drops():
    """Animate floating drops and expire old ones."""
    now = time.time()
    to_discard = []

    for idx, drop in enumerate(world_drops):
        age_secs = now - drop['spawn_time']
        if age_secs >= drop_lifetime:
            to_discard.append(idx)
            continue

        drop['entity'].y = drop['base_y'] + 0.08 * sin(age_secs * 2.0)

    for idx in reversed(to_discard):
        world_drops[idx]['entity'].disable()
        del world_drops[idx]

sky = Sky(color=color.rgb(0.7, 0.8, 1.0))
ambient_light = AmbientLight(color=color.white, strength=1.0)


def _apply_day_night_phase(phase):
    # 40s day / 20s night on a 60s loop
    day_fraction = 40.0 / 60.0
    night_start = day_fraction
    dusk_start = night_start - 0.10
    dusk_end = night_start

    if phase < dusk_start:
        sunlight = 1.0
    elif phase < dusk_end:
        t = (phase - dusk_start) / (dusk_end - dusk_start)
        sunlight = max(0.0, 1.0 - t)
    elif phase < 1.0:
        sunlight = 0.0
    else:
        sunlight = 0.0

    night_strength = 1.0 - sunlight
    brightness = 0.03 + 0.97 * sunlight

    sky.color = color.rgb(0.10 + 0.65 * sunlight, 0.16 + 0.55 * sunlight, 0.30 + 0.70 * sunlight)
    ambient_light.color = color.rgb(0.15 + 0.75 * sunlight, 0.20 + 0.65 * sunlight, 0.30 + 0.70 * sunlight)
    ambient_light.strength = brightness

    # make night substantially darker; use a gentle evening fade only near the transition
    if phase >= night_start:
        sky.color = color.rgb(0.02, 0.04, 0.10)
        ambient_light.color = color.rgb(0.07, 0.09, 0.13)
        ambient_light.strength = 0.05

# ── simulation status HUD ─────────────────────────────────────────────────────
current_cycle = 0
current_season = 'spring'
current_day = 0

# track previous day/season phase to detect transitions
_prev_is_day = True
_prev_season = current_season

status_text = Text(
    text='Spring | Cycle 0\nDay',
    parent=camera.ui,
    position=(-0.78, 0.47),
    scale=0.7,
    origin=(-0.5, 0),
    color=color.white,
    background=False,
)
clock_text = status_text  # alias so existing update calls still work


def _apply_season(season_name):
    global current_season
    current_season = season_name
    season_data = seasons[season_name]
    chunk.moisture = season_data['moisture']
    chunk.fertility = season_data['fertility']
    terrain.texture = load_texture(f'textures/{season_data["texture"]}')
    status_text.text = f'{season_name.title()} | Cycle {current_cycle}\nDay'

_apply_season(current_season)

VEGETATION_TYPES = VEGETATION_BLOCK_IDS


def _count_kind_near(x, z, kind, radius):
    count = 0
    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            if chunk.get_block(x + dx, SY, z + dz) == kind:
                count += 1
    return count


def _sim_step():
    global current_cycle

    current_cycle += 1
    season_names = list(seasons)
    if current_cycle % season_length == 0:
        season_index = season_names.index(current_season)
        next_index = (season_index + 1) % len(season_names)
        new_season = season_names[next_index]
        _apply_season(new_season)
        _on_season_start(new_season)
    else:
        _apply_season(current_season)

    # ── age decay: applies to all entities tagged 'flora' ────────────────────
    flora_defs = _veg_with_tag('flora')
    flora_bids = {v['block_id'] for v in flora_defs}

    changes = {}
    for x in range(sx):
        for z in range(sz):
            bid = chunk.get_block(x, SY, z)
            if bid not in flora_bids:
                continue
            vdef = veg_defs[bid]
            age = chunk.vegetation_ages.get((x, z), vdef['initial_age'])
            decay_every = vdef.get('age_decay_every_n_cycles', 1)
            if current_cycle % decay_every == 0 and random.randint(0, 100) >= chunk.moisture:
                age -= 1
                if age <= 0:
                    changes[(x, z)] = GRASS
                else:
                    changes[(x, z)] = bid
                    chunk.vegetation_ages[(x, z)] = age

    # ── spawn: only on empty grass, driven by flora tag and spawn rules ───────
    # pre-build tag->block_ids map for spawn constraint resolution
    tag_to_bids = {}
    for v in entities_cfg.get('vegetation', []):
        for t in v.get('tags', []):
            tag_to_bids.setdefault(t, set()).add(v['block_id'])

    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) in VEGETATION_TYPES:
                continue
            if random.randint(0, 100) > chunk.fertility:
                continue
            if (x, z) in changes:
                continue
            # try each flora entity in priority order (order in entities.json)
            for vdef in flora_defs:
                sp = vdef['spawn']
                if random.random() >= sp['chance']:
                    continue
                blocked = False
                # generic tag-based proximity constraints
                for key, radius in sp.items():
                    if not key.startswith('requires_no_') or not radius:
                        continue
                    # key = 'requires_no_<tag>_within'
                    tag = key[len('requires_no_'):-len('_within')]
                    for constrain_bid in tag_to_bids.get(tag, set()):
                        if _count_kind_near(x, z, constrain_bid, radius) > 0:
                            blocked = True
                            break
                    if blocked:
                        break
                if blocked:
                    continue
                max_same = sp.get('max_same_within')
                if max_same and _count_kind_near(x, z, vdef['block_id'], max_same['radius']) >= max_same['count']:
                    continue
                changes[(x, z)] = vdef['block_id']
                chunk.vegetation_ages[(x, z)] = vdef['initial_age']
                break

    for (x, z), bid in changes.items():
        if bid == GRASS:
            # vegetation died — spawn its drop using the age it had
            old_bid = chunk.get_block(x, SY, z)
            if old_bid in veg_defs:
                age = chunk.vegetation_ages.get((x, z))
                _drop_from(veg_defs[old_bid], x, z, age=age)
            chunk.vegetation_ages.pop((x, z), None)
        chunk.set_block(x, SY, z, bid)
        if bid != GRASS:
            chunk.vegetation_ages[(x, z)] = chunk.vegetation_ages.get(
                (x, z), veg_defs[bid]['initial_age']
            )

    # rebuild meshes for all flora entities
    for vdef in flora_defs:
        bid = vdef['block_id']
        for si in range(len(vdef['stages'])):
            veg_entities[(bid, si)].model = build_veg_mesh(bid, si)

    counts = {vdef['name']: sum(1 for x in range(sx) for z in range(sz)
                                if chunk.get_block(x, SY, z) == vdef['block_id'])
              for vdef in flora_defs}
    print(f'[sim] cycle={current_cycle} season={current_season} ' +
          '  '.join(f"{k}={v}" for k, v in counts.items()))
    status_text.text = f'{current_season.title()} | Cycle {current_cycle}\n{"Day" if _is_daytime((time.time() % day_night_cycle) / day_night_cycle) else "Night"}'

# ── simulation timers ─────────────────────────────────────────────────────────
_sim_timer = 0.0
_creature_timers = [0.0] * len(creature_defs)


def _is_daytime(phase):
    return phase < (40.0 / 60.0)


def _manhattan(x1, z1, x2, z2):
    return abs(x1 - x2) + abs(z1 - z2)


def _rebuild_flora_meshes():
    for vdef in _veg_with_tag('flora'):
        bid = vdef['block_id']
        for si in range(len(vdef['stages'])):
            veg_entities[(bid, si)].model = build_veg_mesh(bid, si)


def _compute_creature_needs(cdef, st):
    """Return {need_name: priority_value} for the creature's configured needs."""
    needs = {}
    init_hunger = cdef.get('initial_hunger', 3)
    for need in cdef.get('needs', []):
        if need == 'feed':
            needs['feed'] = max(0, init_hunger - st['hunger'])
    return needs


def _pick_highest_need(needs):
    if not needs:
        return None
    task, value = max(needs.items(), key=lambda kv: kv[1])
    return task if value > 0 else None


def _is_flower_at(x, z):
    return flower_vdef and chunk.get_block(x, SY, z) == flower_vdef['block_id']


def _is_flower_dead(x, z):
    if not _is_flower_at(x, z):
        return False
    age = chunk.vegetation_ages.get((x, z), flower_vdef['initial_age'])
    return age <= flower_vdef['stages'][0]['max_age']


def _eat_food_at_block(x, z, ci, i, cdef):
    edible = _resolve_diet(cdef)
    if not edible:
        return False

    st = all_creature_stats[ci][i]
    max_hunger = cdef.get('initial_hunger', 3)
    hunger_gain = cdef.get('hunger_per_food', 1)
    if st['hunger'] >= max_hunger:
        return False

    to_remove = []
    ate = False
    eaten_items = []
    for idx, drop in enumerate(world_drops):
        if drop['item'] not in edible or drop['x'] != x or drop['z'] != z:
            continue
        while drop['count'] > 0 and st['hunger'] < max_hunger:
            st['hunger'] += hunger_gain
            drop['count'] -= 1
            ate = True
            eaten_items.append(drop['item'])
        if drop['count'] <= 0:
            to_remove.append(idx)

    for idx in reversed(to_remove):
        world_drops[idx]['entity'].disable()
        del world_drops[idx]

    if ate:
        items = ', '.join(sorted(set(eaten_items)))
        print(f'[feed] {cdef["name"]}#{i} ate {items} at ({x},{z}) hunger={st["hunger"]}')
    return ate


def _attack_flower_at(x, z, attack_value):
    if not _is_flower_at(x, z):
        return False

    age = chunk.vegetation_ages.get((x, z), flower_vdef['initial_age'])
    age -= attack_value
    if age <= 0:
        _drop_from(flower_vdef, x, z, age=age)
        chunk.set_block(x, SY, z, GRASS)
        chunk.vegetation_ages.pop((x, z), None)
    else:
        chunk.vegetation_ages[(x, z)] = age
    _rebuild_flora_meshes()
    return True


def _find_nearest_food_drop(x, z, cdef, radius=5):
    edible = _resolve_diet(cdef)
    if not edible:
        return None
    best = None
    best_dist = None
    for drop in world_drops:
        if drop['item'] not in edible:
            continue
        dist = _manhattan(x, z, drop['x'], drop['z'])
        if dist == 0 or dist > radius:
            continue
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = (drop['x'], drop['z'])
    return best


def _find_nearest_flower(x, z, dead, radius=5):
    if not flower_vdef:
        return None
    flower_bid = flower_vdef['block_id']
    best = None
    best_dist = None
    for fx in range(max(0, x - radius), min(sx, x + radius + 1)):
        for fz in range(max(0, z - radius), min(sz, z + radius + 1)):
            if chunk.get_block(fx, SY, fz) != flower_bid:
                continue
            is_dead = _is_flower_dead(fx, fz)
            if is_dead != dead:
                continue
            dist = _manhattan(x, z, fx, fz)
            if dist == 0 or dist > radius:
                continue
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = (fx, fz)
    return best


def _step_toward(x, z, tx, tz, avoids):
    candidates = []
    for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        nx = x + dx
        nz = z + dz
        if not (0 <= nx < sx and 0 <= nz < sz):
            continue
        if avoids and chunk.get_block(nx, SY, nz) in avoids:
            continue
        candidates.append((nx, nz, _manhattan(nx, nz, tx, tz)))
    if not candidates:
        return None
    min_dist = min(c[2] for c in candidates)
    best = [c for c in candidates if c[2] == min_dist]
    nx, nz, _ = random.choice(best)
    return (nx, nz)


def _move_creature_random(x, z, avoids):
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    random.shuffle(dirs)
    for dx, dz in dirs:
        nx = min(max(x + dx, 0), sx - 1)
        nz = min(max(z + dz, 0), sz - 1)
        if avoids and chunk.get_block(nx, SY, nz) in avoids:
            continue
        return (nx, nz)
    return (x, z)


def _act_feed(ci, i, cdef, x, z, avoids):
    st = all_creature_stats[ci][i]

    if _eat_food_at_block(x, z, ci, i, cdef):
        return (x, z)

    if _is_flower_at(x, z):
        _attack_flower_at(x, z, st['attack'])
        print(f'[feed] {cdef["name"]}#{i} attacked flower at ({x},{z})')
        return (x, z)

    food_target = _find_nearest_food_drop(x, z, cdef)
    if food_target:
        step = _step_toward(x, z, food_target[0], food_target[1], avoids)
        return step if step else (x, z)

    dead_target = _find_nearest_flower(x, z, dead=True)
    if dead_target:
        step = _step_toward(x, z, dead_target[0], dead_target[1], avoids)
        return step if step else (x, z)

    live_target = _find_nearest_flower(x, z, dead=False)
    if live_target:
        step = _step_toward(x, z, live_target[0], live_target[1], avoids)
        return step if step else (x, z)

    return _move_creature_random(x, z, avoids)


def _creature_move(ci, i, cdef, x, z, avoids):
    st = all_creature_stats[ci][i]
    needs = _compute_creature_needs(cdef, st)
    task = _pick_highest_need(needs)
    if task == 'feed':
        return _act_feed(ci, i, cdef, x, z, avoids)
    return _move_creature_random(x, z, avoids)


def _spawn_creature_at(ci, x, z):
    """Spawn one new instance of creature ci at world position (x, z)."""
    cdef  = creature_defs[ci]
    y_off = cdef.get('y_offset', 1.0)
    sx_c, sz_c = cdef.get('scale', [1.0, 1.0])
    ent = Entity(
        model='quad',
        texture=load_texture(cdef['texture']),
        position=(x, SY + y_off, z),
        scale=(sx_c, sz_c),
        billboard=True,
        double_sided=True,
        collider=None,
    )
    ent.setTransparency(1)
    all_creature_positions[ci].append((x, z))
    all_creature_entities[ci].append(ent)
    all_creature_stats[ci].append({
        'age':    cdef.get('initial_age', 1),
        'hunger': cdef.get('initial_hunger', 3),
        'attack': cdef.get('attack', 0),
    })


def _remove_creature(ci, i):
    """Remove creature instance i of type ci from the world."""
    cdef = creature_defs[ci]
    x, z = all_creature_positions[ci][i]
    _drop_from(cdef, x, z)
    all_creature_entities[ci][i].disable()
    del all_creature_positions[ci][i]
    del all_creature_entities[ci][i]
    del all_creature_stats[ci][i]


def _on_day_start():
    """Called once at the start of each day cycle."""
    for ci, cdef in _creatures_with_tag('fauna'):
        stats = all_creature_stats[ci]
        to_remove = []
        for i, st in enumerate(stats):
            if st['hunger'] > 0:
                st['hunger'] -= 1
            else:
                st['age'] -= 1
            if st['age'] <= 0:
                to_remove.append(i)
        for i in reversed(to_remove):
            _remove_creature(ci, i)

        surviving = all_creature_stats[ci]
        print(f'[day] {cdef["name"]} count={len(surviving)}  ' +
              '  '.join(f'#{i} age={s["age"]} hunger={s["hunger"]}'
                        for i, s in enumerate(surviving)))


def _on_season_start(season_name):
    """Called once when a new season begins."""
    for ci, cdef in _creatures_with_tag('fauna'):
        stats = all_creature_stats[ci]
        to_remove = []

        if season_name == 'winter':
            # every fauna loses 1 age at the start of winter
            for i, st in enumerate(stats):
                st['age'] -= 1
                if st['age'] <= 0:
                    to_remove.append(i)
            for i in reversed(to_remove):
                _remove_creature(ci, i)

        elif season_name == 'summer':
            # reproduce: spawn reproduce_count new instances near each existing creature
            lo, hi = cdef.get('reproduce_count', [1, 1])
            positions_snapshot = list(all_creature_positions[ci])
            for x, z in positions_snapshot:
                for _ in range(random.randint(lo, hi)):
                    # place offspring on same block as parent
                    _spawn_creature_at(ci, x, z)


def update():
    global _sim_timer, _creature_timers, _prev_is_day, _creature_timers, current_day
    has_focus = getattr(window, 'has_focus', True)
    if not has_focus:
        if mouse.locked:
            mouse.locked = False
            mouse.visible = True
        return

    if not mouse.locked:
        mouse.locked = True
        mouse.visible = False

    player.x = min(max(player.x, 0.5), sx - 1.5)
    player.z = min(max(player.z, 0.5), sz - 1.5)

    global config, cycle_length, season_length, day_night_cycle, drop_lifetime, seasons, SIM_INTERVAL
    if CONFIG_PATH.exists():
        new_config = load_config()
        if new_config != config:
            config = new_config
            cycle_length = float(config['cycle_length'])
            season_length = int(config['season_length'])
            day_night_cycle = float(config['day_night_cycle'])
            drop_lifetime = float(config['drop_lifetime'])
            seasons = config['seasons']
            SIM_INTERVAL = cycle_length

    phase = (time.time() % day_night_cycle) / day_night_cycle
    _apply_day_night_phase(phase)
    is_day = _is_daytime(phase)
    status_text.text = f'{current_season.title()} | Cycle {current_cycle} | Day {current_day}\n{"Day" if is_day else "Night"}'

    # detect day start (night→day transition)
    if is_day and not _prev_is_day:
        current_day += 1
        _on_day_start()
    _prev_is_day = is_day

    # ── move all 'fauna' tagged creatures ────────────────────────────────────
    for ci, cdef in _creatures_with_tag('fauna'):
        moves_at_night = cdef.get('moves_at_night', False)
        interval = cdef.get('move_interval_day', 3.0)
        if is_day or moves_at_night:
            _creature_timers[ci] += time.dt
            if _creature_timers[ci] >= interval:
                _creature_timers[ci] = 0.0
                avoids = _resolve_avoids(cdef)
                positions = all_creature_positions[ci]
                entities  = all_creature_entities[ci]
                y_off = cdef.get('y_offset', 1.0)
                for i, (x, z) in enumerate(positions):
                    nx, nz = _creature_move(ci, i, cdef, x, z, avoids)
                    if (nx, nz) != (x, z):
                        positions[i] = (nx, nz)
                        entities[i].x = nx
                        entities[i].z = nz
                        entities[i].y = SY + y_off
        else:
            _creature_timers[ci] = 0.0

    _update_drops()

    _sim_timer += time.dt
    if _sim_timer >= SIM_INTERVAL:
        _sim_timer = 0.0
        _sim_step()


def input(key):
    if not getattr(window, 'has_focus', True):
        return
    if key == 'escape':
        print(f'Saving world to {WORLD_FILE} …')
        chunk.save(WORLD_FILE)
        print('Saved.')
        application.quit()

app.run()

