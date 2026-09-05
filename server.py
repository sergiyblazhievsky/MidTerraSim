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

from chunk import Chunk, AIR, GRASS, GRASS_PATCH

WORLD_FILE = 'chunks/chunk_0_0.wrld'
CONFIG_PATH = Path(__file__).with_name('config.json')
ENTITIES_PATH = Path(__file__).with_name('entities.json')

# Season a brand-new world starts in, and the fraction of each day/night
# cycle that counts as daytime (40 of every 60 units).
DEFAULT_SEASON = 'spring'
DAY_FRACTION = 40.0 / 60.0

# How far a hungry creature looks for food when its entities.json definition
# doesn't say (`feed_radius`). Manhattan tiles.
DEFAULT_FEED_RADIUS = 5
DEFAULT_STOCK_NEED = 0.9

DEFAULT_CONFIG = {
    'cycle_length': 300.0,
    'season_length': 10,
    'day_night_cycle': 60.0,
    'drop_lifetime': 60.0,
    'seasons': {
        'spring': {'moisture': 40, 'fertility': 20, 'texture': 'soil.png'},
        'summer': {'moisture': 20, 'fertility': 30, 'texture': 'soil.png'},
        'fall': {'moisture': 30, 'fertility': 40, 'texture': 'soil_fall.png'},
        'winter': {'moisture': 30, 'fertility': 10, 'texture': 'soil_winter.png'},
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
        self.crop_vdefs = self._veg_with_tag('crops')
        self.creature_defs = list(self.entities_cfg.get('creatures', []))
        self.structure_defs = list(self.entities_cfg.get('structures', []))
        self.structure_defs_by_name = {s['name']: s for s in self.structure_defs}
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

        # ── admin-controlled runtime state ─────────────────────────────────
        # Not persisted to config.json or the world file -- intentionally
        # transient, reset to the default on every server restart. Scales
        # everything time-based (day/night, creature movement/needs ticks,
        # the flora sim cycle, periodic saves, and item-drop aging) uniformly
        # via an accumulated virtual-time offset added on top of the real
        # wall clock -- see _effective_time(). Initialized before anything
        # that reads _effective_time(), e.g. _restore_clock() below.
        self.speed_multiplier = 1
        self._time_offset = 0.0

        self._terrain_texture = 'textures/soil.png'
        self._restore_clock()

        self.all_creature_positions = []
        self.all_creature_stats = []
        self._creature_timers = [0.0] * len(self.creature_defs)
        self._next_creature_id = 0
        restored_names = self._load_or_seed_creatures()

        self.world_structures = []
        self._next_structure_id = 0
        self._restore_structures()

        self.world_drops = []
        self._next_drop_id = 0

        self._sim_timer = 0.0
        self._save_timer = 0.0
        self.revision = 0
        self.vegetation_revision = 0
        self.structure_revision = 0

        print(f'[server] world loaded: {self.sx}x{self.sy}x{self.sz} (surface_y={self.SY})')
        clock_source = 'resumed' if self.chunk.season else 'fresh'
        print(f'[server] {clock_source} clock: season={self.current_season} '
              f'cycle={self.current_cycle} day={self.current_day}')
        for ci, cdef in enumerate(self.creature_defs):
            source = 'restored' if cdef['name'] in restored_names else 'spawned'
            print(f'[server] {source} {len(self.all_creature_positions[ci])}x {cdef["name"]}')
        if self.world_structures:
            print(f'[server] restored {len(self.world_structures)} structure(s)')

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

    def _resolve_plant_diet_tiers(self, cdef):
        """The plants this creature eats standing, best tier first."""
        return self._plant_tiers(cdef.get('diet', []))

    def _plant_tiers(self, entries):
        """Entries that match vegetation by name or tag, in declared order,
        each as the group of plants that entry names -- a tag like "crops"
        covers several. Preference runs *between* tiers; inside one, distance
        decides, so a nearer cabbage beats a further carrot."""
        tiers = []
        seen = set()
        for entry in entries:
            tier = []
            for vdef in self.entities_cfg.get('vegetation', []):
                if vdef.get('name') != entry and entry not in vdef.get('tags', []):
                    continue
                bid = vdef['block_id']
                if bid in seen:
                    continue
                seen.add(bid)
                tier.append(vdef)
            if tier:
                tiers.append(tier)
        return tiers

    def _forage_tiers(self, cdef):
        """Every standing plant this creature works for food, best tier first,
        each paired with whether it eats the plant itself.

        `diet` plants are food outright: grass is cropped away, a bush or crop
        is bitten back, and either way the mouthful is the meal. `forage`
        plants it merely knocks down, eating whatever loot spills on a later
        tick -- which is how a rat gets a seed out of a flower. Declaring both
        means eating comes first."""
        return ([(tier, True) for tier in self._resolve_plant_diet_tiers(cdef)]
                + [(tier, False) for tier in self._plant_tiers(cdef.get('forage', []))])

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

    # ── simulation clock ──────────────────────────────────────────────────
    def _restore_clock(self):
        """Resume the cycle/season/day counters saved in the world file.

        The day/night phase itself is *not* restored: it is derived from the
        wall clock, so there is nothing meaningful to carry across a restart.
        _prev_is_day is seeded from the phase the world is actually in right
        now rather than assumed to be day, so a server started at night still
        counts the dawn that follows (assuming daytime would swallow it, since
        the night->day edge never fires)."""
        self.current_cycle = max(0, self.chunk.cycle)
        self.current_day = max(0, self.chunk.day)

        season = self.chunk.season
        if season is not None and season not in self.seasons:
            print(f'[server] saved season {season!r} is not in config.json; '
                  f'falling back to {DEFAULT_SEASON!r}')
            season = None
        self.current_season = season or DEFAULT_SEASON

        self._phase = (self._effective_time() % self.day_night_cycle) / self.day_night_cycle
        self._prev_is_day = self._phase < DAY_FRACTION
        self._apply_season(self.current_season)

    def _store_clock_in_chunk(self):
        """Mirror the clock into the chunk so the next save persists it."""
        self.chunk.cycle = self.current_cycle
        self.chunk.season = self.current_season
        self.chunk.day = self.current_day

    # ── season ────────────────────────────────────────────────────────────
    def _apply_season(self, season_name):
        self.current_season = season_name
        season_data = self.seasons[season_name]
        self.chunk.moisture = season_data['moisture']
        self.chunk.fertility = season_data['fertility']
        self._terrain_texture = f'textures/{season_data["texture"]}'

    # ── creature seeding / persistence ────────────────────────────────────
    def _seed_creature_type(self, cdef):
        """Random starting placement for one creature definition."""
        positions = []
        min_dist = cdef.get('min_spawn_distance', 2)
        avoids = self._resolve_avoids(cdef)
        count = cdef.get('count', 1)

        for _ in range(count):
            for _ in range(200):
                x = random.randint(0, self.sx - 1)
                z = random.randint(0, self.sz - 1)
                surface = self.chunk.get_block(x, self.SY, z)
                # Bare soil or decorative grass cover are valid spawn tiles.
                surface_ok = surface in (GRASS, GRASS_PATCH)
                block_ok = surface not in avoids
                spaced = all(abs(x - rx) + abs(z - rz) >= min_dist for rx, rz in positions)
                if surface_ok and block_ok and spaced:
                    positions.append((x, z))
                    break
            else:
                positions.append((random.randint(0, self.sx - 1), random.randint(0, self.sz - 1)))

        stats = []
        for _ in positions:
            self._next_creature_id += 1
            stats.append(self._new_creature_stats(cdef))
        return positions, stats

    def _new_creature_stats(self, cdef):
        """Fresh per-individual state. `home` is the id of the structure this
        creature dwells in, None while it has none."""
        return {
            'id': self._next_creature_id,
            'age': cdef.get('initial_age', 1),
            'hunger': cdef.get('initial_hunger', 3),
            'attack': cdef.get('attack', 0),
            'sleep': 0.0,
            'asleep': False,
            'home': None,
            'home_need': 0.0,
            'carrying': None,
        }

    def _seed_creatures(self):
        for cdef in self.creature_defs:
            positions, stats = self._seed_creature_type(cdef)
            self.all_creature_positions.append(positions)
            self.all_creature_stats.append(stats)

    def _restore_creature_type(self, cdef):
        """Rebuild one creature definition's live state from the world file."""
        positions = []
        stats = []
        for inst in self.chunk.creatures.get(cdef['name'], []):
            x = min(max(inst['x'], 0), self.sx - 1)
            z = min(max(inst['z'], 0), self.sz - 1)
            positions.append((x, z))
            stats.append({
                'id': inst['id'],
                'age': inst.get('age', cdef.get('initial_age', 1)),
                'hunger': inst.get('hunger', cdef.get('initial_hunger', 3)),
                'attack': inst.get('attack', cdef.get('attack', 0)),
                'sleep': inst.get('sleep', 0.0),
                'asleep': inst.get('asleep', False),
                'home': inst.get('home'),
                'home_need': inst.get('home_need', 0.0),
                'carrying': inst.get('carrying'),
            })
        return positions, stats

    def _load_or_seed_creatures(self):
        """Restore fauna saved in the world file; seed any definition the file
        doesn't know about, so adding a species to entities.json doesn't
        require regenerating the world. Returns the restored type names."""
        self._next_creature_id = self.chunk.next_creature_id
        restored_names = set()

        for cdef in self.creature_defs:
            if cdef['name'] in self.chunk.creatures:
                positions, stats = self._restore_creature_type(cdef)
                restored_names.add(cdef['name'])
            else:
                positions, stats = self._seed_creature_type(cdef)
            self.all_creature_positions.append(positions)
            self.all_creature_stats.append(stats)

        return restored_names

    def _store_creatures_in_chunk(self):
        """Mirror live fauna into the chunk so the next save persists it."""
        self.chunk.creatures = {
            cdef['name']: [
                {
                    'id': st['id'], 'x': x, 'z': z,
                    'age': st['age'], 'hunger': st['hunger'],
                    'attack': st['attack'],
                    'sleep': float(st.get('sleep', 0.0)),
                    'asleep': bool(st.get('asleep', False)),
                    'home': st.get('home'),
                    'home_need': float(st.get('home_need', 0.0)),
                    'carrying': st.get('carrying'),
                }
                for (x, z), st in zip(self.all_creature_positions[ci],
                                      self.all_creature_stats[ci])
            ]
            for ci, cdef in enumerate(self.creature_defs)
        }
        # Never write a counter below a live id, or a reload could hand out
        # an id that is already in use.
        highest = max((st['id'] for stats in self.all_creature_stats
                       for st in stats), default=0)
        self.chunk.next_creature_id = max(self._next_creature_id, highest)

    # ── structures (burrows) ──────────────────────────────────────────────
    def _restore_structures(self):
        """Load structures saved in the world file. Types that no longer exist
        in entities.json are dropped rather than kept as unrenderable ghosts."""
        self._next_structure_id = self.chunk.next_structure_id
        for inst in self.chunk.structures:
            if inst['type'] not in self.structure_defs_by_name:
                print(f'[server] dropping saved structure of unknown type '
                      f'{inst["type"]!r}')
                continue
            self.world_structures.append({
                'id': inst['id'],
                'type': inst['type'],
                'x': min(max(inst['x'], 0), self.sx - 1),
                'z': min(max(inst['z'], 0), self.sz - 1),
                'age': inst.get('age', self._structure_initial_age(inst['type'])),
                'contains': list(inst.get('contains', [])),
            })
        self._evict_dangling_homes()

    def _evict_dangling_homes(self):
        """Clear any creature whose saved home points at a structure that is
        no longer around, so it wants a new one instead of a dead id."""
        live_ids = {s['id'] for s in self.world_structures}
        for stats in self.all_creature_stats:
            for st in stats:
                if st.get('home') is not None and st['home'] not in live_ids:
                    st['home'] = None

    def _store_structures_in_chunk(self):
        """Mirror live structures into the chunk so the next save persists them."""
        self.chunk.structures = [
            {
                'id': s['id'], 'type': s['type'], 'x': s['x'], 'z': s['z'],
                'age': s['age'], 'contains': list(s['contains']),
            }
            for s in self.world_structures
        ]
        highest = max((s['id'] for s in self.world_structures), default=0)
        self.chunk.next_structure_id = max(self._next_structure_id, highest)

    def _structure_initial_age(self, type_name):
        sdef = self.structure_defs_by_name.get(type_name, {})
        return int(sdef.get('initial_age', 1))

    def _structure_at(self, x, z):
        """The structure occupying this tile, if any -- only one may."""
        for s in self.world_structures:
            if s['x'] == x and s['z'] == z:
                return s
        return None

    def _structure_by_id(self, structure_id):
        if structure_id is None:
            return None
        for s in self.world_structures:
            if s['id'] == structure_id:
                return s
        return None

    def _can_dwell(self, cdef, sdef):
        """Whether this creature is one of the structure's dwellers, matched by
        creature name or tag (same name-or-tag rule as diets)."""
        dwellers = sdef.get('dwellers', [])
        if cdef['name'] in dwellers:
            return True
        return any(self._has_tag(cdef, entry) for entry in dwellers)

    def _resolve_home_structure(self, cdef):
        """The structure definition this creature builds as its home."""
        for sdef in self.structure_defs:
            if self._can_dwell(cdef, sdef):
                return sdef
        return None

    def _build_structure(self, sdef, x, z):
        self._next_structure_id += 1
        structure = {
            'id': self._next_structure_id,
            'type': sdef['name'],
            'x': x, 'z': z,
            'age': int(sdef.get('initial_age', 1)),
            'contains': [],
        }
        self.world_structures.append(structure)
        self.structure_revision += 1
        return structure

    def _remove_structure(self, index):
        """Remove a structure and evict its dwellers, so anything that called
        it home starts wanting a new one instead of pointing at a dead id."""
        structure = self.world_structures.pop(index)
        for stats in self.all_creature_stats:
            for st in stats:
                if st.get('home') == structure['id']:
                    st['home'] = None
        self.structure_revision += 1
        print(f'[structure] {structure["type"]}#{structure["id"]} collapsed at '
              f'({structure["x"]},{structure["z"]})')
        return structure

    def _break_structures(self):
        """Season start: each structure may weather a hit and eventually
        collapse. `break_chance` and the age it burns through are per-type."""
        for index in reversed(range(len(self.world_structures))):
            structure = self.world_structures[index]
            sdef = self.structure_defs_by_name.get(structure['type'], {})
            if random.random() >= float(sdef.get('break_chance', 0.0)):
                continue
            structure['age'] -= 1
            self.structure_revision += 1
            if structure['age'] <= 0:
                self._remove_structure(index)
            else:
                print(f'[structure] {structure["type"]}#{structure["id"]} damaged '
                      f'at ({structure["x"]},{structure["z"]}) age={structure["age"]}')

    def _spawn_creature_at(self, ci, x, z):
        cdef = self.creature_defs[ci]
        self._next_creature_id += 1
        self.all_creature_positions[ci].append((x, z))
        self.all_creature_stats[ci].append(self._new_creature_stats(cdef))

    def _remove_creature(self, ci, i):
        cdef = self.creature_defs[ci]
        x, z = self.all_creature_positions[ci][i]
        self._drop_carried(ci, i, x, z)
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
                        # A creature that ate nothing (already full) leaves the
                        # drop alone, and one that ate part of a stack leaves
                        # the rest -- same rule as eating deliberately.
                        if gained == 0:
                            continue
                        drop['count'] -= gained
                        print(f'[pickup] {cdef["name"]}#{i} picked up {gained}x {drop["item"]} '
                              f'at ({drop["x"]},{drop["z"]}) hunger={stats[i]["hunger"]}')
                        picked_up = True
                        break
                if picked_up:
                    break

            if drop['count'] <= 0:
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

    def _attack_plant_at(self, x, z, vdef, attack_value):
        """Damage a standing plant: age it by `attack_value`, and spill its
        stage loot if that kills it. No hunger changes hands -- the attacker
        feeds on whatever drops (how rats work flowers, and now crops)."""
        if not self._is_veg_at(x, z, vdef):
            return False
        age = self.chunk.vegetation_ages.get((x, z), vdef['initial_age'])
        age -= attack_value
        if age <= 0:
            self._drop_from(vdef, x, z, age=age)
            self.chunk.set_block(x, self.SY, z, GRASS)
            self.chunk.vegetation_ages.pop((x, z), None)
        else:
            self.chunk.vegetation_ages[(x, z)] = age
        self.vegetation_revision += 1
        return True

    def _attack_flower_at(self, x, z, attack_value):
        return self._attack_plant_at(x, z, self.flower_vdef, attack_value)

    def _crop_at(self, x, z):
        """The crop standing on this tile, if any."""
        return self._veg_at(x, z, self.crop_vdefs)

    def _veg_at(self, x, z, vdefs):
        """Whichever of these plants stands on this tile, if any. Only one
        can, so the order they are offered in never decides anything."""
        for vdef in vdefs:
            if self._is_veg_at(x, z, vdef):
                return vdef
        return None

    def _is_veg_at(self, x, z, vdef):
        return bool(vdef) and self.chunk.get_block(x, self.SY, z) == vdef['block_id']

    def _eat_ground_cover_at(self, x, z, ci, i, cdef, vdef):
        """Remove ground-cover (grass) on this tile and restore hunger."""
        if not self._is_veg_at(x, z, vdef):
            return False
        st = self.all_creature_stats[ci][i]
        max_hunger = cdef.get('initial_hunger', 3)
        if st['hunger'] >= max_hunger:
            return False
        hunger_gain = cdef.get('hunger_per_food', 1)
        self.chunk.set_block(x, self.SY, z, GRASS)
        self.chunk.vegetation_ages.pop((x, z), None)
        st['hunger'] += hunger_gain
        self.vegetation_revision += 1
        print(f'[feed] {cdef["name"]}#{i} ate {vdef["name"]} at ({x},{z}) '
              f'hunger={st["hunger"]}')
        return True

    def _browse_plant_at(self, x, z, ci, i, cdef, vdef):
        """Eat from a standing plant (bush, crop): damage it, gain hunger."""
        st = self.all_creature_stats[ci][i]
        attack = st.get('attack', cdef.get('attack', 1))
        if not self._attack_plant_at(x, z, vdef, attack):
            return False
        if st['hunger'] < cdef.get('initial_hunger', 3):
            st['hunger'] += cdef.get('hunger_per_food', 1)
        print(f'[feed] {cdef["name"]}#{i} browsed {vdef["name"]} at ({x},{z}) '
              f'age={self.chunk.vegetation_ages.get((x, z), 0)} '
              f'hunger={st["hunger"]}')
        return True

    def _find_nearest_veg_among(self, x, z, vdefs, radius=DEFAULT_FEED_RADIUS):
        """Nearest tile holding any one of these plants. Which type it is
        doesn't break the tie -- distance does."""
        bids = {v['block_id'] for v in vdefs if v}
        if not bids:
            return None
        best = None
        best_dist = None
        for fx in range(max(0, x - radius), min(self.sx, x + radius + 1)):
            for fz in range(max(0, z - radius), min(self.sz, z + radius + 1)):
                if self.chunk.get_block(fx, self.SY, fz) not in bids:
                    continue
                dist = self._manhattan(x, z, fx, fz)
                if dist == 0 or dist > radius:
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = (fx, fz)
        return best

    def _find_nearest_forage(self, x, z, tier, radius=DEFAULT_FEED_RADIUS):
        """Nearest plant of this tier worth walking to. Distance decides,
        except among flowers: a dried one is a seed head about to fall, so it
        outranks a live one however much further away it stands."""
        if tier and all(self._has_tag(v, 'flower') for v in tier):
            for dead in (True, False):
                target = self._find_nearest_flower(x, z, dead=dead, radius=radius)
                if target:
                    return target
            return None
        return self._find_nearest_veg_among(x, z, tier, radius=radius)

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

    @staticmethod
    def _feed_radius(cdef):
        """How far this creature searches for food, in Manhattan tiles."""
        try:
            return max(0, int(cdef.get('feed_radius', DEFAULT_FEED_RADIUS)))
        except (TypeError, ValueError):
            return DEFAULT_FEED_RADIUS

    def _find_nearest_food_drop(self, x, z, cdef, radius=DEFAULT_FEED_RADIUS):
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

    def _find_nearest_flower(self, x, z, dead, radius=DEFAULT_FEED_RADIUS):
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

    def _work_plant_at(self, x, z, ci, i, cdef, tier, eats):
        """Take what this creature can from whichever plant of this tier
        stands on its tile. Eaten plants hand over a mouthful -- ground cover
        cropped away whole, anything else bitten back -- while a raided one is
        only knocked down, its loot left on the ground to be eaten later."""
        vdef = self._veg_at(x, z, tier)
        if not vdef:
            return False
        if not eats:
            st = self.all_creature_stats[ci][i]
            self._attack_plant_at(x, z, vdef, st['attack'])
            print(f'[feed] {cdef["name"]}#{i} attacked {vdef["name"]} at ({x},{z})')
            return True
        if self._has_tag(vdef, 'ground_cover'):
            return self._eat_ground_cover_at(x, z, ci, i, cdef, vdef)
        return self._browse_plant_at(x, z, ci, i, cdef, vdef)

    def _step_or_stay(self, x, z, target, avoids):
        """Head for a target, holding position when nothing is walkable."""
        step = self._step_toward(x, z, target[0], target[1], avoids)
        return step if step else (x, z)

    def _act_feed(self, ci, i, cdef, x, z, avoids):
        """One feed order for every creature: eat drops -- underfoot, then the
        nearest in reach -- and failing that work the standing plants, best
        diet tier first, taking one underfoot before walking to the nearest.

        Tier order beats distance, so a rabbit crosses the field for a carrot
        instead of settling for the grass it is standing on. Only when a tier
        has nothing at all in range does the next one get a look.

        What separates a rat from a rabbit here is only their data -- which
        drops each eats, and which plants each works."""
        radius = self._feed_radius(cdef)

        if self._eat_food_at_block(x, z, ci, i, cdef):
            return (x, z)

        target = self._find_nearest_food_drop(x, z, cdef, radius=radius)
        if target:
            return self._step_or_stay(x, z, target, avoids)

        for tier, eats in self._forage_tiers(cdef):
            if self._work_plant_at(x, z, ci, i, cdef, tier, eats):
                return (x, z)
            target = self._find_nearest_forage(x, z, tier, radius=radius)
            if target:
                return self._step_or_stay(x, z, target, avoids)

        return self._move_creature_random(x, z, avoids)

    def _compute_creature_needs(self, cdef, st):
        needs = {}
        init_hunger = cdef.get('initial_hunger', 3)
        for need in cdef.get('needs', []):
            if need == 'feed':
                needs['feed'] = max(0, init_hunger - st['hunger'])
            elif need == 'sleep':
                needs['sleep'] = 0 if st.get('asleep', False) else st.get('sleep', 0.0)
            elif need == 'home':
                # Satisfied outright while it has somewhere to dwell; the want
                # only accrues (at day start) while homeless.
                needs['home'] = 0 if st.get('home') else st.get('home_need', 0.0)
            elif need == 'stock':
                # Constant, unlike the others: hoarding is never satisfied and
                # never urgent. Its value only decides where it sits in the
                # pecking order -- below any real hunger (>= 1), above a home
                # want that hasn't had a couple of days to build up.
                needs['stock'] = self._stock_need(cdef)
        return needs

    @staticmethod
    def _stock_need(cdef):
        try:
            return float(cdef.get('stock_need', DEFAULT_STOCK_NEED))
        except (TypeError, ValueError):
            return DEFAULT_STOCK_NEED

    @staticmethod
    def _ranked_needs(needs):
        """Tasks worth acting on, strongest first. Ties keep the order they
        were declared in. A task may decline the turn (return None) and hand
        over to the next one down -- see `_act_stock`."""
        ranked = sorted(needs.items(), key=lambda kv: kv[1], reverse=True)
        return [task for task, value in ranked if value > 0]

    def _wake_creature(self, ci, i, cdef):
        st = self.all_creature_stats[ci][i]
        st['asleep'] = False
        st['sleep'] = 0.0

    def _sleep_creature(self, ci, i, cdef):
        st = self.all_creature_stats[ci][i]
        st['asleep'] = True

    def _settle_home(self, ci, i, structure):
        st = self.all_creature_stats[ci][i]
        st['home'] = structure['id']
        st['home_need'] = 0.0

    def _act_home(self, ci, i, cdef, x, z, avoids):
        """Claim a home on the tile the creature is standing on: adopt the
        structure already there if this creature dwells in that kind, else
        build one. Only one structure may occupy a tile."""
        sdef = self._resolve_home_structure(cdef)
        if sdef is None:
            return self._move_creature_random(x, z, avoids)

        who = f'{cdef["name"]}#{self.all_creature_stats[ci][i]["id"]}'
        existing = self._structure_at(x, z)
        if existing is not None:
            existing_def = self.structure_defs_by_name.get(existing['type'])
            if existing_def and self._can_dwell(cdef, existing_def):
                self._settle_home(ci, i, existing)
                print(f'[home] {who} moved into '
                      f'{existing["type"]}#{existing["id"]} at ({x},{z})')
                return (x, z)
            # Tile is taken by something it can't live in — look elsewhere.
            return self._move_creature_random(x, z, avoids)

        structure = self._build_structure(sdef, x, z)
        self._settle_home(ci, i, structure)
        print(f'[home] {who} built '
              f'{structure["type"]}#{structure["id"]} at ({x},{z})')
        return (x, z)

    # ── stocking the larder ───────────────────────────────────────────────
    def _pick_up_drop_at(self, x, z, cdef):
        """Take a single item off an edible drop on this tile. Returns the item
        name, or None if there's nothing here this creature would hoard."""
        edible = self._resolve_diet(cdef)
        if not edible:
            return None
        for idx, drop in enumerate(self.world_drops):
            if drop['item'] not in edible or drop['x'] != x or drop['z'] != z:
                continue
            drop['count'] -= 1
            if drop['count'] <= 0:
                del self.world_drops[idx]
            return drop['item']
        return None

    def _act_stock(self, ci, i, cdef, x, z, avoids):
        """Fetch one edible drop back to the larder. Returns None to decline
        the turn -- with no home to stock or nothing in reach worth carrying,
        the creature should get on with its next need instead."""
        st = self.all_creature_stats[ci][i]
        if self._structure_by_id(st.get('home')) is None:
            return None

        item = self._pick_up_drop_at(x, z, cdef)
        if item:
            st['carrying'] = item
            print(f'[stock] {cdef["name"]}#{st["id"]} picked up {item} '
                  f'at ({x},{z})')
            return (x, z)

        target = self._find_nearest_food_drop(x, z, cdef, self._feed_radius(cdef))
        if not target:
            return None
        step = self._step_toward(x, z, target[0], target[1], avoids)
        return step if step else (x, z)

    def _stash_carried(self, ci, i, cdef, structure):
        """Move the carried item off the creature and into the larder."""
        st = self.all_creature_stats[ci][i]
        item = st.get('carrying')
        st['carrying'] = None
        for entry in structure['contains']:
            if entry['item'] == item:
                entry['count'] += 1
                break
        else:
            structure['contains'].append({'item': item, 'count': 1})
        self.structure_revision += 1
        print(f'[stock] {cdef["name"]}#{st["id"]} stashed {item} in '
              f'{structure["type"]}#{structure["id"]}')

    def _drop_carried(self, ci, i, x, z):
        """Put the carried item back on the ground -- the creature died, or
        lost the home it was hauling towards."""
        st = self.all_creature_stats[ci][i]
        item = st.get('carrying')
        st['carrying'] = None
        if item:
            self._spawn_drop(item, 1, x, z)

    def _act_deliver(self, ci, i, cdef, x, z, avoids):
        """Haul the carried item home. Runs instead of the needs pass, so a
        loaded creature won't stop to eat, sleep or dig on the way."""
        st = self.all_creature_stats[ci][i]
        structure = self._structure_by_id(st.get('home'))
        if structure is None:
            # Home collapsed mid-haul; put it down rather than carry forever.
            self._drop_carried(ci, i, x, z)
            return (x, z)

        if (x, z) == (structure['x'], structure['z']):
            self._stash_carried(ci, i, cdef, structure)
            return (x, z)

        step = self._step_toward(x, z, structure['x'], structure['z'], avoids)
        if not step:
            return (x, z)
        if step == (structure['x'], structure['z']):
            self._stash_carried(ci, i, cdef, structure)
        return step

    def _creature_move(self, ci, i, cdef, x, z, avoids):
        st = self.all_creature_stats[ci][i]
        if st.get('carrying'):
            return self._act_deliver(ci, i, cdef, x, z, avoids)

        needs = self._compute_creature_needs(cdef, st)
        for task in self._ranked_needs(needs):
            moved = self._act_on_need(task, ci, i, cdef, x, z, avoids)
            if moved is not None:
                return moved
        return self._move_creature_random(x, z, avoids)

    def _act_on_need(self, task, ci, i, cdef, x, z, avoids):
        """Run one need's behavior. None means "declined, try the next need"."""
        if task == 'sleep':
            self._sleep_creature(ci, i, cdef)
            return (x, z)
        if task == 'feed':
            return self._act_feed(ci, i, cdef, x, z, avoids)
        if task == 'home':
            return self._act_home(ci, i, cdef, x, z, avoids)
        if task == 'stock':
            return self._act_stock(ci, i, cdef, x, z, avoids)
        return None

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

            # Wanting a home builds up day by day for as long as it lacks one.
            if 'home' in cdef.get('needs', []):
                home_gain = cdef.get('home_gain', 0.5)
                for st in self.all_creature_stats[ci]:
                    if st.get('home') is None:
                        st['home_need'] = st.get('home_need', 0.0) + home_gain

            surviving = self.all_creature_stats[ci]
            print(f'[day] {cdef["name"]} count={len(surviving)}  ' +
                  '  '.join(f'#{i} age={s["age"]} hunger={s["hunger"]}'
                            for i, s in enumerate(surviving)))

    def _on_season_start(self, season_name):
        # Weather the structures first: a creature evicted this season starts
        # wanting a new home right away.
        self._break_structures()

        for ci, cdef in self._creatures_with_tag('fauna'):
            # Winter ages fauna first so only survivors breed this season.
            if season_name == 'winter':
                to_remove = []
                for i, st in enumerate(self.all_creature_stats[ci]):
                    st['age'] -= 1
                    if st['age'] <= 0:
                        to_remove.append(i)
                for i in reversed(to_remove):
                    self._remove_creature(ci, i)

            # Every season change: each living individual produces offspring.
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
        # Ground-cover (grass patches) is a decorative under-layer: it must
        # not occupy a tile against flower/bush/tree. Those may spawn on top
        # of (and replace) a grass patch. Grass itself only colonizes bare soil.
        ground_cover_bids = {v['block_id'] for v in self._veg_with_tag('ground_cover')}
        occupying_bids = flora_bids - ground_cover_bids

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
                bid = self.chunk.get_block(x, self.SY, z)
                if bid in occupying_bids:
                    continue
                if random.randint(0, 100) > self.chunk.fertility:
                    continue
                if (x, z) in changes:
                    continue
                for vdef in flora_defs:
                    sp = vdef['spawn']
                    active_seasons = sp.get('active_seasons')
                    if active_seasons and self.current_season not in active_seasons:
                        continue
                    is_cover = vdef['block_id'] in ground_cover_bids
                    # Grass only on bare soil; other flora may take over a grass patch.
                    if is_cover and bid != GRASS:
                        continue
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
        is_day = self._phase < DAY_FRACTION

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
        self._store_creatures_in_chunk()
        self._store_structures_in_chunk()
        self._store_clock_in_chunk()
        self.chunk.save(WORLD_FILE)
        counts = ', '.join(f'{len(self.all_creature_positions[ci])} {cdef["name"]}'
                           for ci, cdef in enumerate(self.creature_defs))
        if self.world_structures:
            counts += f', {len(self.world_structures)} structures'
        print(f'[server] saved world to {WORLD_FILE}' + (f' ({counts})' if counts else ''))

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
                    'home': st.get('home'),
                    'carrying': st.get('carrying'),
                    'needs': self._compute_creature_needs(cdef, st),
                })

        drops = [
            {
                'id': d['id'], 'item': d['item'], 'count': d['count'],
                'x': d['x'], 'z': d['z'], 'age': now - d['spawn_time'],
            }
            for d in self.world_drops
        ]

        structures = [
            {
                'id': s['id'], 'type': s['type'],
                'x': s['x'], 'z': s['z'],
                'age': s['age'], 'contains': list(s['contains']),
            }
            for s in self.world_structures
        ]

        return {
            'revision': self.revision,
            'vegetation_revision': self.vegetation_revision,
            'structure_revision': self.structure_revision,
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
            'structures': structures,
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
    // Grass patches cover nearly every bare tile and drown out the
    // interesting flora in this debug view -- filter them out of the
    // inspector only (they still exist in /state for the UI client).
    const flora = state.vegetation.filter(v => v.type !== 'grass');
    const groups = groupBy(flora, v => v.type);
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
    return details('veg', 'Vegetation', flora.length, html, 'section');
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
        cinner += leaf(`<span class="kv">home</span>: ${c.home === null || c.home === undefined ? '&mdash;' : '#' + c.home}`);
        cinner += leaf(`<span class="kv">carrying</span>: ${c.carrying ? esc(c.carrying) : '&mdash;'}`);
        cinner += renderNeeds(ikey, c.needs);
        inner += details(ikey, `${name} #${c.id}`, undefined, cinner);
      }
      html += details(`creature:${name}`, name, items.length, inner);
    }
    return details('creatures', 'Creatures', state.creatures.length, html, 'section');
  }

  function renderStructures(state) {
    const all = state.structures || [];
    const groups = groupBy(all, s => s.type);
    let html = '';
    for (const name of Object.keys(groups).sort()) {
      const items = groups[name].slice().sort((a, b) => a.id - b.id);
      let inner = '';
      for (const s of items) {
        const dwellers = (state.creatures || []).filter(c => c.home === s.id);
        let sinner = '';
        sinner += leaf(`<span class="kv">position</span>: (${s.x}, ${s.z})`);
        sinner += leaf(`<span class="kv">age</span>: ${s.age}`);
        sinner += leaf(`<span class="kv">dwellers</span>: ${dwellers.length ? dwellers.map(c => esc(c.type) + ' #' + c.id).join(', ') : '&mdash;'}`);
        sinner += leaf(`<span class="kv">contains</span>: ${s.contains && s.contains.length ? esc(s.contains.map(e => e.count + 'x ' + e.item).join(', ')) : '&mdash;'}`);
        inner += details(`structure:${name}:${s.id}`, `${name} #${s.id}`, undefined, sinner);
      }
      html += details(`structure:${name}`, name, items.length, inner);
    }
    return details('structures', 'Structures', all.length, html, 'section');
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
        renderVegetation(state) + renderStructures(state)
        + renderCreatures(state) + renderDrops(state);

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
