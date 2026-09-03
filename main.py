"""
MidTerraSim — UI client.

Thin, disposable presentation layer. It owns no authoritative simulation
state and runs no simulation timers — all of that lives in `server.py`. This
process only:

  * polls the server's HTTP/JSON API for renderable snapshots (on a
    background thread, so a slow/unavailable server never freezes rendering)
  * rebuilds/updates Ursina entities (terrain, vegetation, creatures, drops)
    from those snapshots
  * runs local, purely-visual first-person player controls, camera, HUD,
    and day/night lighting

If the server is unreachable or disconnects, the window stays open, shows a
clear "disconnected" status, and keeps retrying automatically — reconnecting
resumes rendering without restarting either process. Closing this window
(Esc or the window's close button) never touches the server: it does not
save, and it does not shut the server down.

Controls: WASD to move, mouse to look, Shift to sprint, Space to jump,
Esc to quit the UI (server keeps running).
"""

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request
from math import sin
from pathlib import Path
from ursina import *
from ursina.prefabs.first_person_controller import FirstPersonController

CONFIG_PATH = Path(__file__).with_name('config.json')
ENTITIES_PATH = Path(__file__).with_name('entities.json')

DEFAULT_CLIENT_CONFIG = {
    'host': '127.0.0.1',
    'port': 8765,
    'poll_interval': 0.15,
    'request_timeout': 2.0,
}


def load_client_config(path=CONFIG_PATH):
    """Read only the bits main.py needs: the `client` section of config.json.
    The server owns the rest of config.json (seasons/timing/etc.)."""
    cfg = dict(DEFAULT_CLIENT_CONFIG)
    if path.exists():
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f)
            cfg.update(data.get('client', {}))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def load_entities(path=ENTITIES_PATH):
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


entities_cfg = load_entities()
veg_defs = {v['block_id']: v for v in entities_cfg.get('vegetation', [])}
item_defs = {i['name']: i for i in entities_cfg.get('items', [])}
creature_defs_by_name = {c['name']: c for c in entities_cfg.get('creatures', [])}
item_texture_paths = {name: idef.get('texture') for name, idef in item_defs.items() if idef.get('texture')}

ITEM_TEXTURES = {
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


def _get_stage(vdef, age):
    for stage in vdef['stages']:
        if age <= stage['max_age']:
            return stage
    return vdef['stages'][-1]


# ── networking: background snapshot polling ───────────────────────────────
class ServerClient:
    """Polls GET /state on a daemon thread and hands the latest snapshot to
    the render thread. Never blocks the caller; safe to read from `update()`
    every frame."""

    def __init__(self, base_url, poll_interval, timeout):
        self.base_url = base_url.rstrip('/')
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._lock = threading.Lock()
        self._latest = None
        self._connected = False
        self._received_at = 0.0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True

    def get_latest(self):
        with self._lock:
            return self._latest, self._connected, self._received_at

    def _run(self):
        was_connected = False
        while not self._stop:
            try:
                req = urllib.request.Request(self.base_url + '/state')
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                with self._lock:
                    self._latest = data
                    self._connected = True
                    self._received_at = time.time()
                if not was_connected:
                    print(f'[client] connected to {self.base_url}')
                was_connected = True
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
                with self._lock:
                    self._connected = False
                if was_connected:
                    print(f'[client] disconnected from {self.base_url} ({exc}); retrying…')
                was_connected = False
            time.sleep(self.poll_interval)


# ── mesh helpers ────────────────────────────────────────────────────────────
def _add_top_quad(vl, tl, ul, x, y, z):
    base = len(vl)
    vl += [(x-0.5, y+0.5, z-0.5), (x+0.5, y+0.5, z-0.5),
           (x+0.5, y+0.5, z+0.5), (x-0.5, y+0.5, z+0.5)]
    tl += [base, base+1, base+2, base, base+2, base+3]
    ul += [(0,0),(1,0),(1,1),(0,1)]


def build_ground_mesh(sx, sz, sy):
    """Flat ground plane at the world's surface level (constant `sy`). The
    current world generator always fills the full column height with the
    default ground block (soil), so the topmost block is always at `sy` —
    this mirrors that assumption without needing per-column height data
    from the server."""
    gv, gt, gu = [], [], []
    for x in range(sx):
        for z in range(sz):
            _add_top_quad(gv, gt, gu, x, sy, z)
    return Mesh(vertices=gv, triangles=gt, uvs=gu, mode='triangle')


def build_veg_mesh(entries, sy):
    """Cross-quad mesh for a list of {'x','z','height','width'} vegetation
    entries that all share one (block_id, stage) — one Entity per such group."""
    verts, tris, uvs = [], [], []
    for e in entries:
        x, z, h = e['x'], e['z'], e['height']
        half_w = e.get('width', 1.0) / 2.0
        for (dx0, dz0), (dx1, dz1) in [
            ((-half_w, -half_w), (+half_w, +half_w)),
            ((+half_w, -half_w), (-half_w, +half_w)),
        ]:
            base = len(verts)
            verts += [
                (x+dx0, sy+0.5,   z+dz0),
                (x+dx1, sy+0.5,   z+dz1),
                (x+dx1, sy+0.5+h, z+dz1),
                (x+dx0, sy+0.5+h, z+dz0),
            ]
            tris += [base, base+1, base+2, base, base+2, base+3]
            tris += [base+2, base+1, base, base+3, base+2, base]
            uvs += [(0,0),(1,0),(1,1),(0,1)]
    return Mesh(vertices=verts, triangles=tris, uvs=uvs, mode='triangle')


def build_surface_veg_mesh(entries, sy):
    """Horizontal quads flush with the terrain top — used for ground-cover
    flora (grass patches) that should read as a surface texture on the
    block rather than a vertical plant billboard."""
    verts, tris, uvs = [], [], []
    # Slightly above the soil top face to avoid z-fighting with the ground mesh.
    y = sy + 0.5 + 0.01
    for e in entries:
        x, z = e['x'], e['z']
        base = len(verts)
        verts += [
            (x - 0.5, y, z - 0.5),
            (x + 0.5, y, z - 0.5),
            (x + 0.5, y, z + 0.5),
            (x - 0.5, y, z + 0.5),
        ]
        tris += [base, base + 1, base + 2, base, base + 2, base + 3]
        tris += [base + 2, base + 1, base, base + 3, base + 2, base]
        uvs += [(0, 0), (1, 0), (1, 1), (0, 1)]
    return Mesh(vertices=verts, triangles=tris, uvs=uvs, mode='triangle')


def empty_mesh():
    return Mesh(vertices=[], triangles=[], uvs=[], mode='triangle')


# ── app setup ─────────────────────────────────────────────────────────────────
client_config = load_client_config()

app = Ursina(title='MidTerraSim (client)', borderless=False, fullscreen=False,
             development_mode=False, icon='textures/ursina.ico')
window.fps_counter.enabled = True

sky = Sky(color=color.rgb(0.7, 0.8, 1.0))
ambient_light = AmbientLight(color=color.white, strength=1.0)

# placeholder ground so the player doesn't fall while we wait to connect
_temp_floor = Entity(model='cube', scale=(1000, 1, 1000), position=(0, -0.5, 0),
                      collider='box', visible=False)
# Eye height above the controller's feet (which sit on the ground collider).
# Ursina's FirstPersonController only applies this to camera_pivot at init,
# so we also re-assert camera_pivot.y after world build.
PLAYER_EYE_HEIGHT = 2.0
player = FirstPersonController(position=(0, 2, 0), height=PLAYER_EYE_HEIGHT)
player.camera_pivot.y = PLAYER_EYE_HEIGHT
mouse.visible = False
mouse.locked = True

status_text = Text(
    text='Connecting to server…',
    parent=camera.ui,
    position=(-0.78, 0.47),
    scale=0.7,
    origin=(-0.5, 0),
    color=color.white,
    background=False,
)

disconnected_text = Text(
    text='DISCONNECTED — retrying…',
    parent=camera.ui,
    position=(0, 0.45),
    scale=1.0,
    origin=(0, 0),
    color=color.red,
    background=True,
)
disconnected_text.enabled = False

# ── world state populated once connected ───────────────────────────────────
world_built = False
sx = sz = SY = None
terrain = None
floor = None
veg_entities = {}          # (block_id, stage_index) -> Entity
creature_entities = {}     # creature id -> Entity
creature_last_tex = {}     # creature id -> last texture path (avoid redundant reloads)
drop_entities = {}         # drop id -> Entity
drop_base_xz = {}          # drop id -> (jittered x, jittered z)
drop_ages = {}             # drop id -> age (seconds) as of last snapshot

last_applied_revision = None
cur_snapshot = None
snapshot_received_at = 0.0
last_terrain_texture = None


def _reset_world_state():
    """Tear down all rendered entities so the next snapshot triggers a full
    rebuild. Used when the server's revision goes backwards, i.e. it was
    restarted as a fresh session (not just a network blip)."""
    global world_built, last_applied_revision, cur_snapshot, last_terrain_texture

    if terrain is not None:
        destroy(terrain)
    if floor is not None:
        destroy(floor)
    for ent in veg_entities.values():
        destroy(ent)
    veg_entities.clear()
    for ent in creature_entities.values():
        destroy(ent)
    creature_entities.clear()
    creature_last_tex.clear()
    for ent in drop_entities.values():
        destroy(ent)
    drop_entities.clear()
    drop_base_xz.clear()
    drop_ages.clear()

    world_built = False
    last_applied_revision = None
    cur_snapshot = None
    last_terrain_texture = None
    _apply_snapshot.last_veg_rev = None
    print('[client] server session changed (revision reset) — rebuilding world.')


def _build_world(snap):
    global sx, sz, SY, terrain, floor, last_terrain_texture

    csize = snap['chunk']['size']
    sx, _, sz = csize
    SY = snap['chunk']['surface_y']

    destroy(_temp_floor)

    last_terrain_texture = snap['terrain']['texture']
    terrain = Entity(
        model=build_ground_mesh(sx, sz, SY),
        texture=load_texture(last_terrain_texture),
        collider=None,
    )

    for vdef in entities_cfg.get('vegetation', []):
        bid = vdef['block_id']
        for si, stage in enumerate(vdef['stages']):
            ent = Entity(model=empty_mesh(), texture=load_texture(stage['texture']), collider=None)
            ent.setTransparency(1)
            veg_entities[(bid, si)] = ent

    # Solid walkable collider: default cube is centered, so position.y=SY with
    # scale_y=1 puts the top face at SY+0.5 — flush with the visual ground mesh.
    floor = Entity(
        model='cube',
        scale=(sx, 1, sz),
        position=(sx / 2 - 0.5, SY, sz / 2 - 0.5),
        collider='box',
        visible=False,
    )

    player.height = PLAYER_EYE_HEIGHT
    player.camera_pivot.y = PLAYER_EYE_HEIGHT
    # Spawn above the surface; FirstPersonController gravity will settle
    # the feet onto the floor top (SY+0.5).
    player.position = (sx / 2, SY + 0.5 + PLAYER_EYE_HEIGHT, sz / 2)
    print(f'[client] world built ({sx}x{sz}, surface_y={SY}).')


def _rebuild_vegetation(vegetation_list):
    groups = {}   # (bid, si) -> list of mesh entries
    modes = {}    # (bid, si) -> 'cross' | 'surface'
    for v in vegetation_list:
        bid = v['block_id']
        vdef = veg_defs.get(bid)
        if not vdef:
            continue
        age = v['age'] if v['age'] is not None else vdef['initial_age']
        stage = _get_stage(vdef, age)
        si = vdef['stages'].index(stage)
        key = (bid, si)
        modes[key] = stage.get('render', 'cross')
        groups.setdefault(key, []).append({
            'x': v['x'], 'z': v['z'],
            'height': stage.get('height', 1.0),
            'width': stage.get('width', 1.0),
        })

    for key, ent in veg_entities.items():
        entries = groups.get(key, [])
        if modes.get(key, 'cross') == 'surface':
            ent.model = build_surface_veg_mesh(entries, SY)
        else:
            ent.model = build_veg_mesh(entries, SY)


def _create_drop_entity(item):
    tex_path = item_texture_paths.get(item) or ITEM_TEXTURES.get(item)
    if tex_path:
        ent = Entity(model='quad', texture=load_texture(tex_path), scale=(0.4, 0.4),
                     billboard=True, double_sided=True, collider=None)
        ent.setTransparency(1)
    else:
        col = ITEM_COLORS.get(item, color.white)
        ent = Entity(model='quad', color=col, scale=(0.35, 0.35), billboard=True, collider=None)
    return ent


def _sync_creatures(creatures_list):
    seen = set()
    for c in creatures_list:
        cid = c['id']
        seen.add(cid)
        cdef = creature_defs_by_name.get(c['type'])
        if not cdef:
            continue
        ent = creature_entities.get(cid)
        if ent is None:
            ent = Entity(model='quad', billboard=True, double_sided=True, collider=None)
            ent.setTransparency(1)
            creature_entities[cid] = ent

        y_off = cdef.get('y_offset', 1.0)
        sx_c, sz_c = cdef.get('scale', [1.0, 1.0])
        ent.position = (c['x'], SY + y_off, c['z'])
        ent.scale = (sx_c, sz_c)

        tex_path = cdef.get('sleep_texture', cdef['texture']) if c.get('asleep') else cdef['texture']
        if creature_last_tex.get(cid) != tex_path:
            ent.texture = load_texture(tex_path)
            creature_last_tex[cid] = tex_path

    for cid in list(creature_entities.keys()):
        if cid not in seen:
            creature_entities[cid].disable()
            del creature_entities[cid]
            creature_last_tex.pop(cid, None)


def _sync_drops(drops_list):
    seen = set()
    for d in drops_list:
        did = d['id']
        seen.add(did)
        if did not in drop_entities:
            drop_entities[did] = _create_drop_entity(d['item'])
            drop_base_xz[did] = (d['x'] + random.uniform(-0.3, 0.3), d['z'] + random.uniform(-0.3, 0.3))
        drop_ages[did] = d['age']

    for did in list(drop_entities.keys()):
        if did not in seen:
            drop_entities[did].disable()
            del drop_entities[did]
            drop_base_xz.pop(did, None)
            drop_ages.pop(did, None)


def _apply_snapshot(snap):
    global last_terrain_texture

    tex = snap['terrain']['texture']
    if tex != last_terrain_texture:
        terrain.texture = load_texture(tex)
        last_terrain_texture = tex

    if snap['vegetation_revision'] != _apply_snapshot.last_veg_rev:
        _rebuild_vegetation(snap['vegetation'])
        _apply_snapshot.last_veg_rev = snap['vegetation_revision']

    _sync_creatures(snap['creatures'])
    _sync_drops(snap['drops'])


_apply_snapshot.last_veg_rev = None


def _apply_day_night_phase(phase):
    day_fraction = 40.0 / 60.0
    night_start = day_fraction
    dusk_start = night_start - 0.10
    dusk_end = night_start

    if phase < dusk_start:
        sunlight = 1.0
    elif phase < dusk_end:
        t = (phase - dusk_start) / (dusk_end - dusk_start)
        sunlight = max(0.0, 1.0 - t)
    else:
        sunlight = 0.0

    brightness = 0.03 + 0.97 * sunlight
    sky.color = color.rgb(0.10 + 0.65 * sunlight, 0.16 + 0.55 * sunlight, 0.30 + 0.70 * sunlight)
    ambient_light.color = color.rgb(0.15 + 0.75 * sunlight, 0.20 + 0.65 * sunlight, 0.30 + 0.70 * sunlight)
    ambient_light.strength = brightness

    if phase >= night_start:
        sky.color = color.rgb(0.02, 0.04, 0.10)
        ambient_light.color = color.rgb(0.07, 0.09, 0.13)
        ambient_light.strength = 0.05


def _update_lighting_and_bob(snap, received_at):
    """Runs every frame (not just on new snapshots) so day/night lighting and
    drop bobbing stay smooth between polls, extrapolating from the last
    known server time instead of freezing at poll cadence."""
    t = snap['time']
    day_night_cycle = t['day_night_cycle'] or 60.0
    elapsed = time.time() - received_at
    phase = (t['phase'] + elapsed / day_night_cycle) % 1.0
    is_day = phase < (40.0 / 60.0)
    _apply_day_night_phase(phase)

    for did, ent in drop_entities.items():
        base_age = drop_ages.get(did, 0.0)
        age = base_age + elapsed
        bx, bz = drop_base_xz[did]
        ent.x = bx
        ent.z = bz
        ent.y = SY + 0.52 + 0.08 * sin(age * 2.0)

    status_text.text = (
        f'{t["season"].title()} | Cycle {t["cycle"]} | Day {t["day"]}\n'
        f'{"Day" if is_day else "Night"}'
    )


def input(key):
    if not getattr(window, 'has_focus', True):
        return
    if key == 'escape':
        print('[client] closing UI (server keeps running — nothing is saved from here).')
        server_client.stop()
        application.quit()


def update():
    has_focus = getattr(window, 'has_focus', True)
    if not has_focus:
        if mouse.locked:
            mouse.locked = False
            mouse.visible = True
        return

    if not mouse.locked:
        mouse.locked = True
        mouse.visible = False

    global world_built, last_applied_revision, cur_snapshot, snapshot_received_at

    snap, connected, received_at = server_client.get_latest()
    disconnected_text.enabled = not connected

    if snap is None:
        return  # nothing to render yet — still waiting on first successful poll

    if world_built and last_applied_revision is not None and snap['revision'] < last_applied_revision:
        _reset_world_state()

    if not world_built:
        _build_world(snap)
        world_built = True

    if snap['revision'] != last_applied_revision:
        _apply_snapshot(snap)
        last_applied_revision = snap['revision']
        cur_snapshot = snap
        snapshot_received_at = received_at

    if cur_snapshot is not None:
        _update_lighting_and_bob(cur_snapshot, snapshot_received_at)

    if world_built:
        player.x = min(max(player.x, 0.5), sx - 1.5)
        player.z = min(max(player.z, 0.5), sz - 1.5)


def _parse_args():
    parser = argparse.ArgumentParser(description='MidTerraSim UI client')
    parser.add_argument('--host', default=None, help='Server host (default from config.json)')
    parser.add_argument('--port', type=int, default=None, help='Server port (default from config.json)')
    # Ursina/panda3d may append its own args; ignore anything we don't recognize.
    args, _unknown = parser.parse_known_args()
    return args


_args = _parse_args()
_host = _args.host or client_config['host']
_port = _args.port or client_config['port']
server_client = ServerClient(
    base_url=f'http://{_host}:{_port}',
    poll_interval=client_config['poll_interval'],
    timeout=client_config['request_timeout'],
)
server_client.start()
print(f'[client] polling {_host}:{_port} every {client_config["poll_interval"]}s …')

app.run()
