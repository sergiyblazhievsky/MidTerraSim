"""
MidTerraSim — headless simulation server.

Owns the authoritative world/simulation state and every timer that drives it:
vegetation age/spawn cycles, seasons, day/night, creature movement/needs/
feeding/sleep/lifecycle/reproduction, item drops + expiry, and persistence.

This process has NO Ursina import and does no rendering — it is meant to be
run standalone, in a console, independent of any UI client:

    python server.py

Clients (e.g. main.py) talk to it over a small stdlib-only HTTP/JSON API:

    GET  /health   -> {"status": "ok", "revision": <int>}
    GET  /state    -> full renderable snapshot (see README.md for schema)
    POST /save     -> force an immediate save to disk

The server keeps simulating even if no client ever connects, and continues
running unaffected if a client disconnects or is closed.
"""

import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
    'server': {
        'host': '127.0.0.1',
        'port': 8765,
        'tick_rate': 20.0,
        'save_interval': 120.0,
    },
    # kept here too so `config.json` is a single reference for both processes;
    # the client (main.py) reads this section itself.
    'client': {
        'host': '127.0.0.1',
        'port': 8765,
        'poll_interval': 0.15,
        'request_timeout': 2.0,
    },
}


def load_config(path=CONFIG_PATH):
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding='utf-8')

    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)

    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update({k: v for k, v in data.items() if k not in ('seasons', 'server', 'client')})
    for season_name, default_data in DEFAULT_CONFIG['seasons'].items():
        merged['seasons'].setdefault(season_name, {})
        merged['seasons'][season_name] = {**default_data, **data.get('seasons', {}).get(season_name, {})}
    merged['server'] = {**DEFAULT_CONFIG['server'], **data.get('server', {})}
    merged['client'] = {**DEFAULT_CONFIG['client'], **data.get('client', {})}
    return merged


def load_entities(path=ENTITIES_PATH):
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


class World:
    """Authoritative ecosystem state + simulation rules. Not thread-safe on its
    own — callers must hold `self.lock` around any read or mutation that needs
    a consistent view (the tick loop and the HTTP handlers both do)."""

    def __init__(self):
        self.lock = threading.RLock()

        self._config_mtime = None
        self.config = load_config()
        self._apply_config(self.config)

        self.entities_cfg = load_entities()
        self.veg_defs = {v['block_id']: v for v in self.entities_cfg.get('vegetation', [])}
        self.item_defs = {i['name']: i for i in self.entities_cfg.get('items', [])}
        self.flower_vdef = next((v for v in self.entities_cfg.get('vegetation', [])
                                  if v.get('name') == 'flower'), None)
        self.creature_defs = list(self.entities_cfg.get('creatures', []))
        self._tag_to_bids = {}
        for v in self.entities_cfg.get('vegetation', []):
            for t in v.get('tags', []):
                self._tag_to_bids.setdefault(t, set()).add(v['block_id'])

        if not Path(WORLD_FILE).exists():
            print(f'[server] world file not found: {WORLD_FILE}')
            print('[server] run "python generate_chunk.py" first to create it.')
            sys.exit(1)

        self.chunk = Chunk.load(WORLD_FILE)
        self.sx, self.sy, self.sz = self.chunk.size
        self.SY = self.sy - 1

        self.current_cycle = 0
        self.current_season = 'spring'
        self.current_day = 0
        self._prev_is_day = True
        self._phase = 0.0
        self._terrain_texture = 'textures/grass.png'
        self._apply_season(self.current_season)

        self.all_creature_positions = []
        self.all_creature_stats = []
        self._creature_timers = [0.0] * len(self.creature_defs)
        self._next_creature_id = 0
        self._seed_creatures()

        self.world_drops = []
        self._next_drop_id = 0

        self._sim_timer = 0.0
        self._save_timer = 0.0
        self.revision = 0
        self.vegetation_revision = 0

        # ── admin-controlled runtime state ─────────────────────────────────
        # Not persisted to config.json or the world file -- intentionally
        # transient, reset to the default on every server restart. Scales
        # everything time-based (day/night, creature movement/needs ticks,
        # the flora sim cycle, periodic saves, and item-drop aging) uniformly
        # via an accumulated virtual-time offset added on top of the real
        # wall clock -- see _effective_time().
        self.speed_multiplier = 1
        self._time_offset = 0.0

        print(f'[server] world loaded: {self.sx}x{self.sy}x{self.sz} (surface_y={self.SY})')
        for ci, cdef in enumerate(self.creature_defs):
            print(f'[server] spawned {len(self.all_creature_positions[ci])}x {cdef["name"]}')

    # ── config ────────────────────────────────────────────────────────────
    def _apply_config(self, config):
        self.cycle_length = float(config['cycle_length'])
        self.season_length = int(config['season_length'])
        self.day_night_cycle = float(config['day_night_cycle'])
        self.drop_lifetime = float(config['drop_lifetime'])
        self.seasons = config['seasons']
        self.SIM_INTERVAL = self.cycle_length
        self.server_host = config['server']['host']
        self.server_port = int(config['server']['port'])
        self.tick_rate = float(config['server']['tick_rate'])
        self.save_interval = float(config['server']['save_interval'])

    def maybe_reload_config(self):
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        new_config = load_config()
        if new_config != self.config:
            self.config = new_config
            self._apply_config(self.config)
            # re-apply current season in case its values changed under our feet
            self._apply_season(self.current_season)

    # ── admin: runtime speed control ─────────────────────────────────────
    def _effective_time(self):
        """Wall-clock time plus any accumulated virtual-time bonus from
        speed_multiplier > 1. Equals plain time.time() whenever the
        multiplier is at its default of 1 (offset never leaves 0), so every
        time-based feature in this class can use it unconditionally without
        changing behavior for the common case."""
        return time.time() + self._time_offset

    def set_speed_multiplier(self, value):
        """Set the runtime simulation speed multiplier (1-100). Scales
        day/night, creature movement/needs ticks, the flora sim cycle,
        periodic saves, and item-drop aging uniformly. Transient admin
        state -- not persisted, resets to 1 on server restart."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError('speed_multiplier must be an integer')
        if not (1 <= value <= 100):
            raise ValueError('speed_multiplier must be between 1 and 100')
        self.speed_multiplier = value
        return self.speed_multiplier

    def admin_state(self):
        """Current admin-controlled runtime state, for GET /admin."""
        return {'speed_multiplier': self.speed_multiplier}

    # ── tag / lookup helpers (mirrors original main.py) ──────────────────
    @staticmethod
    def _has_tag(edef, tag):
        return tag in edef.get('tags', [])

    def _veg_with_tag(self, tag):
        return [v for v in self.entities_cfg.get('vegetation', []) if self._has_tag(v, tag)]

    def _creatures_with_tag(self, tag):
        return [(i, c) for i, c in enumerate(self.creature_defs) if self._has_tag(c, tag)]

    def _items_with_tag(self, tag):
        return [name for name, idef in self.item_defs.items() if tag in idef.get('tags', [])]

    def _resolve_diet(self, cdef):
        edible = set()
        for entry in cdef.get('diet', []):
            if entry in self.item_defs:
                edible.add(entry)
            else:
                edible.update(self._items_with_tag(entry))
        return edible

    def _resolve_avoids(self, cdef):
        tag = cdef.get('avoids_block_tag')
        if tag:
            return {v['block_id'] for v in self._veg_with_tag(tag)}
        bid = cdef.get('avoids_block')
        return {bid} if bid is not None else set()

    @staticmethod
    def _get_stage(vdef, age):
        for stage in vdef['stages']:
            if age <= stage['max_age']:
                return stage
        return vdef['stages'][-1]

    @staticmethod
    def _manhattan(x1, z1, x2, z2):
        return abs(x1 - x2) + abs(z1 - z2)

    def _count_kind_near(self, x, z, kind, radius):
        count = 0
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if self.chunk.get_block(x + dx, self.SY, z + dz) == kind:
                    count += 1
        return count

    # ── season ────────────────────────────────────────────────────────────
    def _apply_season(self, season_name):
        self.current_season = season_name
        season_data = self.seasons[season_name]
        self.chunk.moisture = season_data['moisture']
        self.chunk.fertility = season_data['fertility']
        self._terrain_texture = f'textures/{season_data["texture"]}'

    # ── creature seeding ──────────────────────────────────────────────────
    def _seed_creatures(self):
        for cdef in self.creature_defs:
            positions = []
            min_dist = cdef.get('min_spawn_distance', 2)
            avoids = self._resolve_avoids(cdef)
            count = cdef.get('count', 1)

            for _ in range(count):
                for _ in range(200):
                    x = random.randint(0, self.sx - 1)
                    z = random.randint(0, self.sz - 1)
                    block_ok = self.chunk.get_block(x, self.SY, z) not in avoids
                    spaced = all(abs(x - rx) + abs(z - rz) >= min_dist for rx, rz in positions)
                    if self.chunk.get_block(x, self.SY, z) == GRASS and block_ok and spaced:
                        positions.append((x, z))
                        break
                else:
                    positions.append((random.randint(0, self.sx - 1), random.randint(0, self.sz - 1)))

            stats = []
            for _ in positions:
                self._next_creature_id += 1
                stats.append({
                    'id': self._next_creature_id,
                    'age': cdef.get('initial_age', 1),
                    'hunger': cdef.get('initial_hunger', 3),
                    'attack': cdef.get('attack', 0),
                    'sleep': 0.0,
                    'asleep': False,
                })

            self.all_creature_positions.append(positions)
            self.all_creature_stats.append(stats)

    def _spawn_creature_at(self, ci, x, z):
        cdef = self.creature_defs[ci]
        self._next_creature_id += 1
        self.all_creature_positions[ci].append((x, z))
        self.all_creature_stats[ci].append({
            'id': self._next_creature_id,
            'age': cdef.get('initial_age', 1),
            'hunger': cdef.get('initial_hunger', 3),
            'attack': cdef.get('attack', 0),
            'sleep': 0.0,
            'asleep': False,
        })

    def _remove_creature(self, ci, i):
        cdef = self.creature_defs[ci]
        x, z = self.all_creature_positions[ci][i]
        self._drop_from(cdef, x, z)
        del self.all_creature_positions[ci][i]
        del self.all_creature_stats[ci][i]

    # ── drops ─────────────────────────────────────────────────────────────
    def _spawn_drop(self, item, count, x, z):
        self._next_drop_id += 1
        self.world_drops.append({
            'id': self._next_drop_id, 'item': item, 'count': count,
            'x': x, 'z': z, 'spawn_time': self._effective_time(),
        })
        print(f'[drop] {count}x {item} at ({x},{z})')

    def _drop_from(self, edef, x, z, age=None):
        contains = None
        if age is not None and 'stages' in edef:
            stage = self._get_stage(edef, age)
            contains = stage.get('contains')
        if not contains:
            contains = edef.get('contains', [])
        for entry in contains:
            lo, hi = entry['count'][0], entry['count'][1]
            count = random.randint(lo, hi)
            if count > 0:
                self._spawn_drop(entry['item'], count, x, z)

    def _update_drops(self):
        now = self._effective_time()
        to_discard = []

        for idx, drop in enumerate(self.world_drops):
            age_secs = now - drop['spawn_time']
            if age_secs >= self.drop_lifetime:
                to_discard.append(idx)
                continue

            picked_up = False
            for ci, cdef in self._creatures_with_tag('fauna'):
                edible = self._resolve_diet(cdef)
                if drop['item'] not in edible:
                    continue
                positions = self.all_creature_positions[ci]
                stats = self.all_creature_stats[ci]
                max_hunger = cdef.get('initial_hunger', 3)
                hunger_gain = cdef.get('hunger_per_food', 1)
                for i, (cx, cz) in enumerate(positions):
                    if stats[i].get('asleep', False):
                        continue
                    if self._manhattan(cx, cz, drop['x'], drop['z']) <= 1:
                        gained = 0
                        while gained < drop['count'] and stats[i]['hunger'] < max_hunger:
                            stats[i]['hunger'] += hunger_gain
                            gained += 1
                        print(f'[pickup] {cdef["name"]}#{i} picked up {drop["count"]}x {drop["item"]} '
                              f'at ({drop["x"]},{drop["z"]}) hunger={stats[i]["hunger"]}')
                        picked_up = True
                        break
                if picked_up:
                    break

            if picked_up:
                to_discard.append(idx)

        for idx in reversed(to_discard):
            del self.world_drops[idx]

    # ── flora helpers ─────────────────────────────────────────────────────
    def _is_flower_at(self, x, z):
        return self.flower_vdef and self.chunk.get_block(x, self.SY, z) == self.flower_vdef['block_id']

    def _is_flower_dead(self, x, z):
        if not self._is_flower_at(x, z):
            return False
        age = self.chunk.vegetation_ages.get((x, z), self.flower_vdef['initial_age'])
        return age <= self.flower_vdef['stages'][0]['max_age']

    def _attack_flower_at(self, x, z, attack_value):
        if not self._is_flower_at(x, z):
            return False
        age = self.chunk.vegetation_ages.get((x, z), self.flower_vdef['initial_age'])
        age -= attack_value
        if age <= 0:
            self._drop_from(self.flower_vdef, x, z, age=age)
            self.chunk.set_block(x, self.SY, z, GRASS)
            self.chunk.vegetation_ages.pop((x, z), None)
        else:
            self.chunk.vegetation_ages[(x, z)] = age
        self.vegetation_revision += 1
        return True

    def _eat_food_at_block(self, x, z, ci, i, cdef):
        edible = self._resolve_diet(cdef)
        if not edible:
            return False

        st = self.all_creature_stats[ci][i]
        max_hunger = cdef.get('initial_hunger', 3)
        hunger_gain = cdef.get('hunger_per_food', 1)
        if st['hunger'] >= max_hunger:
            return False

        to_remove = []
        ate = False
        eaten_items = []
        for idx, drop in enumerate(self.world_drops):
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
            del self.world_drops[idx]

        if ate:
            items = ', '.join(sorted(set(eaten_items)))
            print(f'[feed] {cdef["name"]}#{i} ate {items} at ({x},{z}) hunger={st["hunger"]}')
        return ate

    def _find_nearest_food_drop(self, x, z, cdef, radius=5):
        edible = self._resolve_diet(cdef)
        if not edible:
            return None
        best = None
        best_dist = None
        for drop in self.world_drops:
            if drop['item'] not in edible:
                continue
            dist = self._manhattan(x, z, drop['x'], drop['z'])
            if dist == 0 or dist > radius:
                continue
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = (drop['x'], drop['z'])
        return best

    def _find_nearest_flower(self, x, z, dead, radius=5):
        if not self.flower_vdef:
            return None
        flower_bid = self.flower_vdef['block_id']
        best = None
        best_dist = None
        for fx in range(max(0, x - radius), min(self.sx, x + radius + 1)):
            for fz in range(max(0, z - radius), min(self.sz, z + radius + 1)):
                if self.chunk.get_block(fx, self.SY, fz) != flower_bid:
                    continue
                is_dead = self._is_flower_dead(fx, fz)
                if is_dead != dead:
                    continue
                dist = self._manhattan(x, z, fx, fz)
                if dist == 0 or dist > radius:
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = (fx, fz)
        return best

    def _step_toward(self, x, z, tx, tz, avoids):
        candidates = []
        for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            nx = x + dx
            nz = z + dz
            if not (0 <= nx < self.sx and 0 <= nz < self.sz):
                continue
            if avoids and self.chunk.get_block(nx, self.SY, nz) in avoids:
                continue
            candidates.append((nx, nz, self._manhattan(nx, nz, tx, tz)))
        if not candidates:
            return None
        min_dist = min(c[2] for c in candidates)
        best = [c for c in candidates if c[2] == min_dist]
        nx, nz, _ = random.choice(best)
        return (nx, nz)

    def _move_creature_random(self, x, z, avoids):
        dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        random.shuffle(dirs)
        for dx, dz in dirs:
            nx = min(max(x + dx, 0), self.sx - 1)
            nz = min(max(z + dz, 0), self.sz - 1)
            if avoids and self.chunk.get_block(nx, self.SY, nz) in avoids:
                continue
            return (nx, nz)
        return (x, z)

    def _act_feed(self, ci, i, cdef, x, z, avoids):
        st = self.all_creature_stats[ci][i]

        if self._eat_food_at_block(x, z, ci, i, cdef):
            return (x, z)

        if self._is_flower_at(x, z):
            self._attack_flower_at(x, z, st['attack'])
            print(f'[feed] {cdef["name"]}#{i} attacked flower at ({x},{z})')
            return (x, z)

        food_target = self._find_nearest_food_drop(x, z, cdef)
        if food_target:
            step = self._step_toward(x, z, food_target[0], food_target[1], avoids)
            return step if step else (x, z)

        dead_target = self._find_nearest_flower(x, z, dead=True)
        if dead_target:
            step = self._step_toward(x, z, dead_target[0], dead_target[1], avoids)
            return step if step else (x, z)

        live_target = self._find_nearest_flower(x, z, dead=False)
        if live_target:
            step = self._step_toward(x, z, live_target[0], live_target[1], avoids)
            return step if step else (x, z)

        return self._move_creature_random(x, z, avoids)

    def _compute_creature_needs(self, cdef, st):
        needs = {}
        init_hunger = cdef.get('initial_hunger', 3)
        for need in cdef.get('needs', []):
            if need == 'feed':
                needs['feed'] = max(0, init_hunger - st['hunger'])
            elif need == 'sleep':
                needs['sleep'] = 0 if st.get('asleep', False) else st.get('sleep', 0.0)
        return needs

    @staticmethod
    def _pick_highest_need(needs):
        if not needs:
            return None
        task, value = max(needs.items(), key=lambda kv: kv[1])
        return task if value > 0 else None

    def _wake_creature(self, ci, i, cdef):
        st = self.all_creature_stats[ci][i]
        st['asleep'] = False
        st['sleep'] = 0.0

    def _sleep_creature(self, ci, i, cdef):
        st = self.all_creature_stats[ci][i]
        st['asleep'] = True

    def _creature_move(self, ci, i, cdef, x, z, avoids):
        st = self.all_creature_stats[ci][i]
        needs = self._compute_creature_needs(cdef, st)
        task = self._pick_highest_need(needs)
        if task == 'sleep':
            self._sleep_creature(ci, i, cdef)
            return (x, z)
        if task == 'feed':
            return self._act_feed(ci, i, cdef, x, z, avoids)
        return self._move_creature_random(x, z, avoids)

    # ── daily / seasonal lifecycle ────────────────────────────────────────
    def _on_day_start(self):
        for ci, cdef in self._creatures_with_tag('fauna'):
            stats = self.all_creature_stats[ci]
            to_remove = []
            for i, st in enumerate(stats):
                if st['hunger'] > 0:
                    st['hunger'] -= 1
                else:
                    st['age'] -= 1
                if st['age'] <= 0:
                    to_remove.append(i)
            for i in reversed(to_remove):
                self._remove_creature(ci, i)

            if 'sleep' in cdef.get('needs', []):
                for i in range(len(self.all_creature_stats[ci])):
                    self._wake_creature(ci, i, cdef)

            surviving = self.all_creature_stats[ci]
            print(f'[day] {cdef["name"]} count={len(surviving)}  ' +
                  '  '.join(f'#{i} age={s["age"]} hunger={s["hunger"]}'
                            for i, s in enumerate(surviving)))

    def _on_season_start(self, season_name):
        for ci, cdef in self._creatures_with_tag('fauna'):
            stats = self.all_creature_stats[ci]
            to_remove = []

            if season_name == 'winter':
                for i, st in enumerate(stats):
                    st['age'] -= 1
                    if st['age'] <= 0:
                        to_remove.append(i)
                for i in reversed(to_remove):
                    self._remove_creature(ci, i)

            elif season_name == 'summer':
                lo, hi = cdef.get('reproduce_count', [1, 1])
                positions_snapshot = list(self.all_creature_positions[ci])
                for x, z in positions_snapshot:
                    for _ in range(random.randint(lo, hi)):
                        self._spawn_creature_at(ci, x, z)

    # ── flora simulation cycle ────────────────────────────────────────────
    def _sim_step(self):
        self.current_cycle += 1
        season_names = list(self.seasons)
        if self.current_cycle % self.season_length == 0:
            season_index = season_names.index(self.current_season)
            next_index = (season_index + 1) % len(season_names)
            new_season = season_names[next_index]
            self._apply_season(new_season)
            self._on_season_start(new_season)
        else:
            self._apply_season(self.current_season)

        flora_defs = self._veg_with_tag('flora')
        flora_bids = {v['block_id'] for v in flora_defs}

        changes = {}
        for x in range(self.sx):
            for z in range(self.sz):
                bid = self.chunk.get_block(x, self.SY, z)
                if bid not in flora_bids:
                    continue
                vdef = self.veg_defs[bid]
                age = self.chunk.vegetation_ages.get((x, z), vdef['initial_age'])
                decay_every = vdef.get('age_decay_every_n_cycles', 1)
                if self.current_cycle % decay_every == 0 and random.randint(0, 100) >= self.chunk.moisture:
                    age -= 1
                    if age <= 0:
                        changes[(x, z)] = GRASS
                    else:
                        changes[(x, z)] = bid
                        self.chunk.vegetation_ages[(x, z)] = age

        for x in range(self.sx):
            for z in range(self.sz):
                if self.chunk.get_block(x, self.SY, z) in self.veg_defs:
                    continue
                if random.randint(0, 100) > self.chunk.fertility:
                    continue
                if (x, z) in changes:
                    continue
                for vdef in flora_defs:
                    sp = vdef['spawn']
                    if random.random() >= sp['chance']:
                        continue
                    blocked = False
                    for key, radius in sp.items():
                        if not key.startswith('requires_no_') or not radius:
                            continue
                        tag = key[len('requires_no_'):-len('_within')]
                        for constrain_bid in self._tag_to_bids.get(tag, set()):
                            if self._count_kind_near(x, z, constrain_bid, radius) > 0:
                                blocked = True
                                break
                        if blocked:
                            break
                    if blocked:
                        continue
                    max_same = sp.get('max_same_within')
                    if max_same and self._count_kind_near(x, z, vdef['block_id'], max_same['radius']) >= max_same['count']:
                        continue
                    changes[(x, z)] = vdef['block_id']
                    self.chunk.vegetation_ages[(x, z)] = vdef['initial_age']
                    break

        for (x, z), bid in changes.items():
            if bid == GRASS:
                old_bid = self.chunk.get_block(x, self.SY, z)
                if old_bid in self.veg_defs:
                    age = self.chunk.vegetation_ages.get((x, z))
                    self._drop_from(self.veg_defs[old_bid], x, z, age=age)
                self.chunk.vegetation_ages.pop((x, z), None)
            self.chunk.set_block(x, self.SY, z, bid)
            if bid != GRASS:
                self.chunk.vegetation_ages[(x, z)] = self.chunk.vegetation_ages.get(
                    (x, z), self.veg_defs[bid]['initial_age']
                )

        if changes:
            self.vegetation_revision += 1

        counts = {vdef['name']: sum(1 for x in range(self.sx) for z in range(self.sz)
                                     if self.chunk.get_block(x, self.SY, z) == vdef['block_id'])
                  for vdef in flora_defs}
        print(f'[sim] cycle={self.current_cycle} season={self.current_season} ' +
              '  '.join(f'{k}={v}' for k, v in counts.items()))

    # ── main tick ─────────────────────────────────────────────────────────
    def tick(self, dt):
        self.maybe_reload_config()

        # speed_multiplier scales every time-based feature uniformly: extra
        # "virtual" time accumulates into _time_offset (on top of the real
        # wall clock _effective_time() reads), and the same scaled dt drives
        # every dt-accumulator below (creature timers, sim/save timers).
        # At the default multiplier of 1 this is a no-op: offset stays 0 and
        # scaled_dt == dt, so behavior is identical to before this feature.
        scaled_dt = dt * self.speed_multiplier
        self._time_offset += scaled_dt - dt

        self._phase = (self._effective_time() % self.day_night_cycle) / self.day_night_cycle
        is_day = self._phase < (40.0 / 60.0)

        if is_day and not self._prev_is_day:
            self.current_day += 1
            self._on_day_start()
        self._prev_is_day = is_day

        for ci, cdef in self._creatures_with_tag('fauna'):
            moves_at_night = cdef.get('moves_at_night', False)
            interval = cdef.get('move_interval_day', 3.0)
            positions = self.all_creature_positions[ci]
            stats = self.all_creature_stats[ci]
            sleep_enabled = 'sleep' in cdef.get('needs', [])

            if is_day:
                self._creature_timers[ci] += scaled_dt
            elif moves_at_night:
                self._creature_timers[ci] += scaled_dt
            elif sleep_enabled:
                self._creature_timers[ci] += scaled_dt
            else:
                self._creature_timers[ci] = 0.0
                continue

            if self._creature_timers[ci] < interval:
                continue
            self._creature_timers[ci] = 0.0

            avoids = self._resolve_avoids(cdef)
            sleep_gain = cdef.get('sleep_gain', 0.5)

            for i, (x, z) in enumerate(positions):
                st = stats[i]

                if sleep_enabled and not is_day and not st.get('asleep', False):
                    st['sleep'] = st.get('sleep', 0.0) + sleep_gain

                if st.get('asleep', False) and not is_day:
                    continue

                nx, nz = self._creature_move(ci, i, cdef, x, z, avoids)
                if (nx, nz) != (x, z):
                    positions[i] = (nx, nz)

        self._update_drops()

        self._sim_timer += scaled_dt
        if self._sim_timer >= self.SIM_INTERVAL:
            self._sim_timer = 0.0
            self._sim_step()

        self._save_timer += scaled_dt
        if self._save_timer >= self.save_interval:
            self._save_timer = 0.0
            self.save()

        self.revision += 1

    # ── persistence ───────────────────────────────────────────────────────
    def save(self):
        self.chunk.save(WORLD_FILE)
        print(f'[server] saved world to {WORLD_FILE}')

    # ── snapshot for API clients ──────────────────────────────────────────
    def snapshot(self):
        now = self._effective_time()
        vegetation = [
            {
                'x': x, 'z': z, 'block_id': bid,
                'type': self.veg_defs[bid]['name'],
                'age': self.chunk.vegetation_ages.get((x, z)),
            }
            for (x, z), bid in self.chunk.overrides_at_y(self.SY).items()
            if bid in self.veg_defs
        ]

        creatures = []
        for ci, cdef in enumerate(self.creature_defs):
            for (x, z), st in zip(self.all_creature_positions[ci], self.all_creature_stats[ci]):
                creatures.append({
                    'id': st['id'],
                    'type': cdef['name'],
                    'x': x, 'z': z,
                    'age': st['age'],
                    'hunger': st['hunger'],
                    'sleep': st.get('sleep', 0.0),
                    'asleep': st.get('asleep', False),
                    'needs': self._compute_creature_needs(cdef, st),
                })

        drops = [
            {
                'id': d['id'], 'item': d['item'], 'count': d['count'],
                'x': d['x'], 'z': d['z'], 'age': now - d['spawn_time'],
            }
            for d in self.world_drops
        ]

        return {
            'revision': self.revision,
            'vegetation_revision': self.vegetation_revision,
            'chunk': {
                'size': list(self.chunk.size),
                'surface_y': self.SY,
            },
            'time': {
                'season': self.current_season,
                'cycle': self.current_cycle,
                'day': self.current_day,
                'is_day': self._prev_is_day,
                'phase': self._phase,
                'day_night_cycle': self.day_night_cycle,
            },
            'terrain': {
                'texture': self._terrain_texture,
            },
            'vegetation': vegetation,
            'creatures': creatures,
            'drops': drops,
        }


# ── HTTP API ────────────────────────────────────────────────────────────────

# A self-contained, dependency-free HTML/JS debug page: polls GET /state and
# renders a collapsible tree of vegetation/creatures/drops, grouped by type,
# with per-creature stats and computed needs. No build step, no external
# assets/CDNs -- works entirely offline, same as the rest of the server.
INSPECTOR_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>MidTerraSim -- Server Inspector</title>
<style>
  body { background:#111418; color:#ddd; font-family: Consolas, Menlo, monospace; font-size: 14px; margin:0; padding: 18px; }
  h1 { font-size: 16px; margin: 0 0 6px 0; color:#8ecbff; }
  #worldinfo { color:#ffd479; font-weight: bold; font-size: 15px; margin-bottom: 4px; }
  #status { color:#8fbf8f; margin-bottom: 14px; font-size: 12px; }
  #status.stale { color:#e77676; }
  #admin-panel { margin: 0 0 16px 0; padding: 10px 12px; border: 1px solid #333; border-radius: 4px; background: #171b20; }
  #admin-panel .title { color:#ffd479; font-weight: bold; margin-right: 10px; }
  #admin-panel input[type=number] { width: 60px; background:#1b1f24; color:#ddd; border:1px solid #444; border-radius: 3px; padding: 2px 4px; font-family: inherit; }
  #admin-panel button { background:#2a3038; color:#ddd; border:1px solid #444; border-radius: 3px; padding: 3px 10px; cursor: pointer; font-family: inherit; }
  #admin-panel button:hover { background:#343b45; }
  #speed-msg { margin-left: 10px; font-size: 12px; }
  details { margin-left: 18px; }
  details > summary { cursor: pointer; padding: 2px 0; list-style: none; }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before { content: "\\25b8  "; color:#777; }
  details[open] > summary::before { content: "\\25be  "; }
  summary:hover { color:#fff; }
  .leaf { margin-left: 34px; padding: 1px 0; color:#bbb; }
  .kv { color:#9fd9a8; }
  .count { color:#888; font-weight: normal; }
  .section > summary { color:#ffd479; font-weight: bold; }
  #tree { margin-top: 8px; }
</style>
</head>
<body>
  <h1>MidTerraSim &mdash; Server Inspector</h1>
  <div id="worldinfo">Season: &ndash; | Cycle: &ndash; | Day: &ndash;</div>
  <div id="status">connecting&hellip;</div>
  <div id="admin-panel">
    <span class="title">Admin</span>
    Speed multiplier: <strong id="speed-current">1</strong>x
    &nbsp;&nbsp;
    <input id="speed-input" type="number" min="1" max="100" step="1" value="1">
    <button id="speed-apply">Apply</button>
    <span id="speed-msg"></span>
  </div>
  <div id="tree"></div>
<script>
  // Track which <details> nodes are open (by stable key) across re-renders,
  // so polling doesn't collapse whatever the user has expanded. The native
  // "toggle" event doesn't bubble, but it does propagate in the capturing
  // phase, so a single document-level capturing listener catches every one.
  const openKeys = new Set();
  document.addEventListener('toggle', (e) => {
    if (e.target.tagName !== 'DETAILS') return;
    const key = e.target.dataset.key;
    if (!key) return;
    if (e.target.open) openKeys.add(key); else openKeys.delete(key);
  }, true);

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function details(key, label, count, innerHtml, extraClass) {
    const isOpen = openKeys.has(key) ? ' open' : '';
    const cls = extraClass ? ` class="${extraClass}"` : '';
    const countHtml = (count === undefined) ? '' : ` <span class="count">(${count})</span>`;
    return `<details data-key="${esc(key)}"${isOpen}${cls}><summary>${esc(label)}${countHtml}</summary>${innerHtml}</details>`;
  }

  function leaf(html) {
    return `<div class="leaf">${html}</div>`;
  }

  function groupBy(list, keyFn) {
    const groups = {};
    for (const item of list) {
      const k = keyFn(item);
      (groups[k] = groups[k] || []).push(item);
    }
    return groups;
  }

  function renderVegetation(state) {
    const groups = groupBy(state.vegetation, v => v.type);
    let html = '';
    for (const name of Object.keys(groups).sort()) {
      const items = groups[name];
      let inner = '';
      for (const v of items) {
        const age = (v.age === null || v.age === undefined) ? 'n/a' : v.age;
        inner += leaf(`${esc(name)} @ (${v.x}, ${v.z}) &mdash; <span class="kv">age</span>: ${age}`);
      }
      html += details(`veg:${name}`, name, items.length, inner);
    }
    return details('veg', 'Vegetation', state.vegetation.length, html, 'section');
  }

  function renderNeeds(parentKey, needs) {
    const entries = Object.entries(needs || {});
    let inner = '';
    for (const [k, v] of entries) {
      inner += leaf(`<span class="kv">${esc(k)}</span>: ${v}`);
    }
    return details(`${parentKey}:needs`, 'needs', entries.length, inner);
  }

  function renderCreatures(state) {
    const groups = groupBy(state.creatures, c => c.type);
    let html = '';
    for (const name of Object.keys(groups).sort()) {
      const items = groups[name].slice().sort((a, b) => a.id - b.id);
      let inner = '';
      for (const c of items) {
        const ikey = `creature:${name}:${c.id}`;
        let cinner = '';
        cinner += leaf(`<span class="kv">position</span>: (${c.x}, ${c.z})`);
        cinner += leaf(`<span class="kv">age</span>: ${c.age}`);
        cinner += leaf(`<span class="kv">hunger</span>: ${c.hunger}`);
        cinner += leaf(`<span class="kv">sleep</span>: ${c.sleep}`);
        cinner += leaf(`<span class="kv">asleep</span>: ${c.asleep}`);
        cinner += renderNeeds(ikey, c.needs);
        inner += details(ikey, `${name} #${c.id}`, undefined, cinner);
      }
      html += details(`creature:${name}`, name, items.length, inner);
    }
    return details('creatures', 'Creatures', state.creatures.length, html, 'section');
  }

  function renderDrops(state) {
    const groups = groupBy(state.drops, d => d.item);
    let html = '';
    for (const name of Object.keys(groups).sort()) {
      const items = groups[name];
      let inner = '';
      for (const d of items) {
        inner += leaf(`${esc(name)} x${d.count} @ (${d.x}, ${d.z}) &mdash; <span class="kv">age</span>: ${d.age.toFixed(1)}s`);
      }
      html += details(`drop:${name}`, name, items.length, inner);
    }
    return details('drops', 'Drops', state.drops.length, html, 'section');
  }

  async function poll() {
    const statusEl = document.getElementById('status');
    try {
      const resp = await fetch('/state', { cache: 'no-store' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const state = await resp.json();

      document.getElementById('tree').innerHTML =
        renderVegetation(state) + renderCreatures(state) + renderDrops(state);

      const t = state.time;
      document.getElementById('worldinfo').textContent =
        `Season: ${t.season.charAt(0).toUpperCase() + t.season.slice(1)}  |  ` +
        `Cycle: ${t.cycle}  |  Day: ${t.day}  |  ${t.is_day ? 'Day' : 'Night'}`;
      statusEl.textContent =
        `revision ${state.revision} | updated ${new Date().toLocaleTimeString()}`;
      statusEl.classList.remove('stale');
    } catch (err) {
      statusEl.textContent = 'disconnected -- retrying\\u2026';
      statusEl.classList.add('stale');
    }

    // keep the "current" admin display in sync even if changed from
    // elsewhere (another browser tab, another admin), without touching
    // whatever the user is currently typing into the input box.
    try {
      const resp = await fetch('/admin', { cache: 'no-store' });
      if (resp.ok) {
        const admin = await resp.json();
        document.getElementById('speed-current').textContent = admin.speed_multiplier;
      }
    } catch (err) { /* keep the last known value on screen */ }
  }

  async function applySpeedMultiplier() {
    const input = document.getElementById('speed-input');
    const msg = document.getElementById('speed-msg');
    const value = parseInt(input.value, 10);
    msg.textContent = '';
    msg.style.color = '#e77676';
    if (!Number.isInteger(value) || value < 1 || value > 100) {
      msg.textContent = 'must be an integer 1-100';
      return;
    }
    try {
      const resp = await fetch('/admin/speed_multiplier', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        msg.textContent = data.error || ('failed (HTTP ' + resp.status + ')');
        return;
      }
      document.getElementById('speed-current').textContent = data.speed_multiplier;
      msg.style.color = '#8fbf8f';
      msg.textContent = 'applied';
      setTimeout(() => { msg.textContent = ''; }, 2000);
    } catch (err) {
      msg.textContent = 'request failed';
    }
  }

  document.getElementById('speed-apply').addEventListener('click', applySpeedMultiplier);
  document.getElementById('speed-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') applySpeedMultiplier();
  });

  poll();
  setInterval(poll, 1000);
</script>
</body>
</html>
"""


def make_handler(world):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'MidTerraSim/1.0'

        def _send_json(self, obj, status=200):
            body = json.dumps(obj).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html, status=200):
            body = html.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self):
            """Read and parse a JSON request body. Returns {} for an empty
            body. Raises ValueError on malformed JSON."""
            length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(length) if length > 0 else b''
            if not raw:
                return {}
            return json.loads(raw.decode('utf-8'))

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/':
                self._send_html(INSPECTOR_HTML)
            elif path == '/health':
                with world.lock:
                    rev = world.revision
                self._send_json({'status': 'ok', 'revision': rev})
            elif path == '/state':
                with world.lock:
                    snap = world.snapshot()
                self._send_json(snap)
            elif path == '/admin':
                with world.lock:
                    state = world.admin_state()
                self._send_json(state)
            else:
                self._send_json({'error': 'not found'}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == '/save':
                with world.lock:
                    world.save()
                    rev = world.revision
                self._send_json({'saved': True, 'revision': rev})
            elif path == '/admin/speed_multiplier':
                try:
                    payload = self._read_json_body()
                    value = payload['value']
                except (ValueError, KeyError):
                    self._send_json(
                        {'error': 'expected a JSON body: {"value": <int 1-100>}'}, 400)
                    return
                try:
                    with world.lock:
                        new_value = world.set_speed_multiplier(value)
                except (TypeError, ValueError) as exc:
                    self._send_json({'error': str(exc)}, 400)
                    return
                print(f'[admin] speed_multiplier set to {new_value}')
                self._send_json({'speed_multiplier': new_value})
            else:
                self._send_json({'error': 'not found'}, 404)

        def log_message(self, fmt, *args):
            pass  # keep console output focused on simulation events

    return Handler


def main():
    world = World()

    parser = argparse.ArgumentParser(description='MidTerraSim headless simulation server.')
    parser.add_argument('--host', default=world.server_host,
                         help=f'HTTP API bind host (default: {world.server_host})')
    parser.add_argument('--port', type=int, default=world.server_port,
                         help=f'HTTP API bind port (default: {world.server_port})')
    args = parser.parse_args()
    bind_host, bind_port = args.host, args.port

    handler_cls = make_handler(world)
    httpd = ThreadingHTTPServer((bind_host, bind_port), handler_cls)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()

    print(f'[server] listening on http://{bind_host}:{bind_port}')
    print('[server] GET /health, GET /state, GET /admin, POST /save, POST /admin/speed_multiplier')
    print('[server] Ctrl+C to save and stop.')

    tick_interval = 1.0 / world.tick_rate
    last = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            dt = now - last
            last = now
            with world.lock:
                world.tick(dt)
            elapsed = time.monotonic() - now
            sleep_for = tick_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        print('\n[server] shutdown requested (Ctrl+C) — saving world…')
    finally:
        with world.lock:
            world.save()
        httpd.shutdown()
        print('[server] stopped.')


if __name__ == '__main__':
    main()
