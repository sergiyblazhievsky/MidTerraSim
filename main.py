"""
MidTerraSim — main entry point.
Loads the chunk from disk, renders terrain + flowers, runs a cellular-automaton
flower simulation on a timed cycle, and saves the world on exit.

Controls: WASD to move, mouse to look, Shift to sprint, Space to jump, Esc to quit.
"""

import json
import random
from math import pi, sin
from pathlib import Path
from ursina import *
from ursina.prefabs.first_person_controller import FirstPersonController
from chunk import Chunk, AIR, GRASS, FLOWER, BUSH, TREE

WORLD_FILE = 'chunks/chunk_0_0.wrld'
CONFIG_PATH = Path(__file__).with_name('config.json')

DEFAULT_CONFIG = {
    'cycle_length': 300.0,
    'season_length': 10,
    'day_night_cycle': 60.0,
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
seasons = config['seasons']
SIM_INTERVAL = cycle_length

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

def build_flower_mesh():
    fv, ft, fu = [], [], []
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) == FLOWER and chunk.vegetation_ages.get((x, z), 2) > 1:
                _add_flower_cross(fv, ft, fu, x, SY, z)
    return Mesh(vertices=fv, triangles=ft, uvs=fu, mode='triangle')


def build_dry_flower_mesh():
    fv, ft, fu = [], [], []
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) == FLOWER and chunk.vegetation_ages.get((x, z), 2) <= 1:
                _add_flower_cross(fv, ft, fu, x, SY, z)
    return Mesh(vertices=fv, triangles=ft, uvs=fu, mode='triangle')


def _add_bush_cross(vl, tl, ul, x, y, z, age):
    bush_height = 1.0 if age >= 5 else 2.0
    for (dx0, dz0), (dx1, dz1) in [
        ((-0.5, -0.5), (+0.5, +0.5)),
        ((+0.5, -0.5), (-0.5, +0.5)),
    ]:
        base = len(vl)
        vl += [
            (x+dx0, y+0.5, z+dz0),
            (x+dx1, y+0.5, z+dz1),
            (x+dx1, y+bush_height, z+dz1),
            (x+dx0, y+bush_height, z+dz0),
        ]
        tl += [base, base+1, base+2, base, base+2, base+3]
        tl += [base+2, base+1, base, base+3, base+2, base]
        ul += [(0,0),(1,0),(1,1),(0,1)]


def build_bush_mesh():
    bv, bt, bu = [], [], []
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) == BUSH and chunk.vegetation_ages.get((x, z), 5) > 1:
                _add_bush_cross(bv, bt, bu, x, SY, z, chunk.vegetation_ages.get((x, z), 5))
    return Mesh(vertices=bv, triangles=bt, uvs=bu, mode='triangle')


def build_dry_bush_mesh():
    bv, bt, bu = [], [], []
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) == BUSH and chunk.vegetation_ages.get((x, z), 5) <= 1:
                _add_bush_cross(bv, bt, bu, x, SY, z, 1)
    return Mesh(vertices=bv, triangles=bt, uvs=bu, mode='triangle')


def _add_tree_cross(vl, tl, ul, x, y, z, age):
    if age >= 9:
        tree_height = 1.0
    elif age >= 6:
        tree_height = 2.0
    else:
        tree_height = 4.0
    for (dx0, dz0), (dx1, dz1) in [
        ((-0.5, -0.5), (+0.5, +0.5)),
        ((+0.5, -0.5), (-0.5, +0.5)),
    ]:
        base = len(vl)
        vl += [
            (x+dx0, y+0.5, z+dz0),
            (x+dx1, y+0.5, z+dz1),
            (x+dx1, y+tree_height, z+dz1),
            (x+dx0, y+tree_height, z+dz0),
        ]
        tl += [base, base+1, base+2, base, base+2, base+3]
        tl += [base+2, base+1, base, base+3, base+2, base]
        ul += [(0,0),(1,0),(1,1),(0,1)]


def build_tree_mesh():
    tv, tt, tu = [], [], []
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) == TREE and chunk.vegetation_ages.get((x, z), 10) > 1:
                _add_tree_cross(tv, tt, tu, x, SY, z, chunk.vegetation_ages.get((x, z), 10))
    return Mesh(vertices=tv, triangles=tt, uvs=tu, mode='triangle')


def build_dead_tree_mesh():
    tv, tt, tu = [], [], []
    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) == TREE and chunk.vegetation_ages.get((x, z), 10) <= 1:
                _add_tree_cross(tv, tt, tu, x, SY, z, 1)
    return Mesh(vertices=tv, triangles=tt, uvs=tu, mode='triangle')

# ── build initial meshes ──────────────────────────────────────────────────────
print('Building meshes …')
terrain = Entity(
    model=build_ground_mesh(),
    texture=load_texture('textures/grass.png'),
    collider=None,
)
flower_entity = Entity(
    model=build_flower_mesh(),
    texture=load_texture('textures/flower_xcross_64.png'),
    collider=None,
)
flower_entity.setTransparency(1)
dry_flower_entity = Entity(
    model=build_dry_flower_mesh(),
    texture=load_texture('textures/flower_dry_xcross_64.png'),
    collider=None,
)
dry_flower_entity.setTransparency(1)
bush_entity = Entity(
    model=build_bush_mesh(),
    texture=load_texture('textures/bush_xcross_64.png'),
    collider=None,
)
bush_entity.setTransparency(1)
dry_bush_entity = Entity(
    model=build_dry_bush_mesh(),
    texture=load_texture('textures/bush_dry_xcross_64.png'),
    collider=None,
)
dry_bush_entity.setTransparency(1)
tree_entity = Entity(
    model=build_tree_mesh(),
    texture=load_texture('textures/tree_xcross_64.png'),
    collider=None,
)
tree_entity.setTransparency(1)
dead_tree_entity = Entity(
    model=build_dead_tree_mesh(),
    texture=load_texture('textures/tree_dry_xcross_64.png'),
    collider=None,
)
dead_tree_entity.setTransparency(1)
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

# ── rats ─────────────────────────────────────────────────────────────────────
rat_positions = []
rat_entities = []

for _ in range(5):
    for _ in range(200):
        x = random.randint(0, sx - 1)
        z = random.randint(0, sz - 1)
        if chunk.get_block(x, SY, z) == GRASS and all(abs(x - rx) + abs(z - rz) >= 3 for rx, rz in rat_positions):
            rat_positions.append((x, z))
            break
    else:
        rat_positions.append((random.randint(0, sx - 1), random.randint(0, sz - 1)))

for x, z in rat_positions:
    rat = Entity(
        model='quad',
        texture=load_texture('textures/rat_64.png'),
        position=(x, SY + 1.0, z),
        scale=(0.8, 1.4),
        billboard=True,
        double_sided=True,
        collider=None,
    )
    rat.setTransparency(1)
    rat_entities.append(rat)

sky = Sky(color=color.rgb(0.7, 0.8, 1.0))

# ── day/night lighting ───────────────────────────────────────────────────────
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

VEGETATION_TYPES = (FLOWER, BUSH, TREE)


def _count_kind_near(x, z, kind, radius):
    count = 0
    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            if chunk.get_block(x + dx, SY, z + dz) == kind:
                count += 1
    return count


def _count_trees_near(x, z, radius=2):
    return _count_kind_near(x, z, TREE, radius)


def _count_bushes_near(x, z, radius=1):
    return _count_kind_near(x, z, BUSH, radius)


def _count_flowers_near(x, z, radius=1):
    return _count_kind_near(x, z, FLOWER, radius)

# ── flower simulation (cellular automaton) ────────────────────────────────────
_sim_timer = 0.0
_rat_move_timer = 0.0


def _is_daytime(phase):
    return phase < (40.0 / 60.0)


def _move_rats():
    for i, (x, z) in enumerate(rat_positions):
        directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        random.shuffle(directions)
        for dx, dz in directions:
            nx = min(max(x + dx, 0), sx - 1)
            nz = min(max(z + dz, 0), sz - 1)
            if chunk.get_block(nx, SY, nz) == TREE:
                continue
            rat_positions[i] = (nx, nz)
            rat_entities[i].x = nx
            rat_entities[i].z = nz
            rat_entities[i].y = SY + 1.0
            break


def _sim_step():
    global current_cycle

    current_cycle += 1
    season_names = list(seasons)
    if current_cycle % season_length == 0:
        season_index = season_names.index(current_season)
        next_index = (season_index + 1) % len(season_names)
        _apply_season(season_names[next_index])
    else:
        _apply_season(current_season)

    _move_rats()

    changes = {}
    for x in range(sx):
        for z in range(sz):
            bid = chunk.get_block(x, SY, z)
            if bid == FLOWER:
                age = chunk.vegetation_ages.get((x, z), 2)
                if random.randint(0, 100) >= chunk.moisture:
                    age -= 1
                    if age <= 0:
                        changes[(x, z)] = GRASS
                    else:
                        changes[(x, z)] = FLOWER
                        chunk.vegetation_ages[(x, z)] = age
            elif bid == BUSH:
                age = chunk.vegetation_ages.get((x, z), 5)
                if current_cycle % 2 == 0 and random.randint(0, 100) >= chunk.moisture:
                    age -= 1
                    if age <= 0:
                        changes[(x, z)] = GRASS
                    else:
                        changes[(x, z)] = BUSH
                        chunk.vegetation_ages[(x, z)] = age
            elif bid == TREE:
                age = chunk.vegetation_ages.get((x, z), 10)
                if current_cycle % 2 == 0 and random.randint(0, 100) >= chunk.moisture:
                    age -= 1
                    if age <= 0:
                        changes[(x, z)] = GRASS
                    else:
                        changes[(x, z)] = TREE
                        chunk.vegetation_ages[(x, z)] = age

    for x in range(sx):
        for z in range(sz):
            if chunk.get_block(x, SY, z) in VEGETATION_TYPES:
                continue
            if random.randint(0, 100) <= chunk.fertility:
                if (x, z) not in changes:
                    if random.random() < 0.010 and _count_trees_near(x, z, 2) < 2 and _count_bushes_near(x, z, 1) == 0:
                        changes[(x, z)] = TREE
                        chunk.vegetation_ages[(x, z)] = 10
                    elif random.random() < 0.030 and _count_bushes_near(x, z, 1) == 0 and _count_trees_near(x, z, 1) == 0:
                        changes[(x, z)] = BUSH
                        chunk.vegetation_ages[(x, z)] = 5
                    elif random.random() < 0.070 and _count_trees_near(x, z, 1) == 0 and _count_bushes_near(x, z, 1) == 0 and _count_flowers_near(x, z, 1) < 2:
                        changes[(x, z)] = FLOWER
                        chunk.vegetation_ages[(x, z)] = 2

    for (x, z), bid in changes.items():
        if bid == GRASS:
            chunk.vegetation_ages.pop((x, z), None)
            chunk.set_block(x, SY, z, GRASS)
        else:
            chunk.set_block(x, SY, z, bid)
            chunk.vegetation_ages[(x, z)] = chunk.vegetation_ages.get((x, z), 2 if bid == FLOWER else 5 if bid == BUSH else 10)

    flower_entity.model = build_flower_mesh()
    dry_flower_entity.model = build_dry_flower_mesh()
    bush_entity.model = build_bush_mesh()
    dry_bush_entity.model = build_dry_bush_mesh()
    tree_entity.model = build_tree_mesh()
    dead_tree_entity.model = build_dead_tree_mesh()
    n_flowers = sum(1 for x in range(sx) for z in range(sz)
                   if chunk.get_block(x, SY, z) == FLOWER)
    n_bushes = sum(1 for x in range(sx) for z in range(sz)
                   if chunk.get_block(x, SY, z) == BUSH)
    n_trees = sum(1 for x in range(sx) for z in range(sz)
                 if chunk.get_block(x, SY, z) == TREE)
    print(f'[sim] cycle: {current_cycle}  season: {current_season}  '
          f'flowers: {n_flowers}  bushes: {n_bushes}  trees: {n_trees}  '
          f'(removed {sum(1 for b in changes.values() if b==GRASS)}'
          f'  added flower {sum(1 for b in changes.values() if b==FLOWER)}'
          f'  bush {sum(1 for b in changes.values() if b==BUSH)}'
          f'  tree {sum(1 for b in changes.values() if b==TREE)})')
    status_text.text = f'{current_season.title()} | Cycle {current_cycle}\n{"Day" if _is_daytime((time.time() % day_night_cycle) / day_night_cycle) else "Night"}'

def update():
    global _sim_timer
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

    global config, cycle_length, season_length, day_night_cycle, seasons, SIM_INTERVAL
    if CONFIG_PATH.exists():
        new_config = load_config()
        if new_config != config:
            config = new_config
            cycle_length = float(config['cycle_length'])
            season_length = int(config['season_length'])
            day_night_cycle = float(config['day_night_cycle'])
            seasons = config['seasons']
            SIM_INTERVAL = cycle_length

    phase = (time.time() % day_night_cycle) / day_night_cycle
    _apply_day_night_phase(phase)
    status_text.text = f'{current_season.title()} | Cycle {current_cycle}\n{"Day" if _is_daytime(phase) else "Night"}'

    global _rat_move_timer
    if _is_daytime(phase):
        _rat_move_timer += time.dt
        if _rat_move_timer >= 3.0:
            _rat_move_timer = 0.0
            _move_rats()
    else:
        _rat_move_timer = 0.0

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

