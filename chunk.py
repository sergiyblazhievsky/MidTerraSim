"""
Chunk API — import this from external editors/tools too.

JSON format:
  {
    "version": 2,
    "size": [100, 100, 100],   # X, Y, Z dimensions
    "fill": "grass",            # default block for every unspecified position
    "overrides": {              # sparse per-block overrides, key = "x,y,z"
      "50,99,50": "air"
    },
    "vegetation_ages": {        # per-column plant age, key = "x,z"
      "50,50": 7
    },
    "creatures": {              # live fauna, keyed by entities.json name
      "rat": [
        {"id": 3, "x": 10, "z": 44, "age": 2, "hunger": 3,
         "attack": 1, "sleep": 0.0, "asleep": false,
         "home": 2, "home_need": 0.0}   # home = structure id, null if none
      ]
    },
    "next_creature_id": 12,     # so ids stay unique across save/load
    "structures": [             # things creatures build, e.g. burrows
      {"id": 2, "type": "burrow", "x": 10, "z": 44, "age": 2, "contains": []}
    ],
    "next_structure_id": 5,
    "time": {                   # simulation clock; season is an entities/config
      "cycle": 41,              # season name, null when never saved
      "season": "fall",
      "day": 6
    }
  }

Version 1 files (no "creatures"/"next_creature_id"/"time") still load; the
caller decides whether to seed fauna and where to start the clock when a
section is absent. "structures" is likewise optional and simply starts empty.
"""

import json
import os

# Block type constants
AIR    = 0
GRASS  = 1
FLOWER = 2
BUSH   = 3
TREE   = 4
GRASS_PATCH = 5   # decorative grass tuft/patch grown on top of bare soil (GRASS)

BLOCK_NAMES = {
    AIR: "air", GRASS: "grass", FLOWER: "flower", BUSH: "bush", TREE: "tree",
    GRASS_PATCH: "grass_patch",
}
BLOCK_IDS   = {v: k for k, v in BLOCK_NAMES.items()}

_DEFAULT_VEGETATION_AGE = {FLOWER: 2, BUSH: 5, TREE: 10, GRASS_PATCH: 5}

FORMAT_VERSION = 2


def _as_int(value, default):
    """Tolerant int coercion for hand-edited save files."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value):
    """For nullable id references (a creature's `home`): null and "" both mean
    "none", anything else has to be a usable int."""
    if value is None or value == '':
        return None
    return int(value)


def _as_list(value):
    if not isinstance(value, list):
        raise ValueError('expected a list')
    return list(value)

# One saved fauna instance: field -> coercion applied on load, so a
# hand-edited or older file can't inject strings into the simulation.
_CREATURE_FIELDS = {
    'id': int, 'x': int, 'z': int, 'age': int, 'hunger': int,
    'attack': int, 'sleep': float, 'asleep': bool,
    'home': _as_optional_int, 'home_need': float,
}

# One saved structure instance (a burrow, and whatever comes later).
# `contains` is the dwellers' larder: persisted, but inert for now.
_STRUCTURE_FIELDS = {
    'id': int, 'type': str, 'x': int, 'z': int, 'age': int,
    'contains': _as_list,
}


class Chunk:
    def __init__(self, size=(100, 100, 100), moisture=0, fertility=0):
        self.size              = tuple(size)
        self.moisture          = moisture   # 0-100
        self.fertility         = fertility  # 0-100
        self._fill             = AIR
        self._overrides        = {}   # {(x, y, z): block_id}
        self.vegetation_ages   = {}  # {(x, z): age}
        # {creature_name: [instance dict, ...]} — see _CREATURE_FIELDS. Empty
        # means "this world has no saved fauna", which is distinct from a
        # world whose fauna all died ({"rat": []}).
        self.creatures         = {}
        self.next_creature_id  = 0
        # [instance dict, ...] — see _STRUCTURE_FIELDS. Creature-built things
        # (burrows) that live on a tile without being blocks.
        self.structures        = []
        self.next_structure_id = 0
        # Simulation clock. season is None until something saves one; the
        # server decides the starting season in that case (and validates a
        # saved name against config.json's seasons).
        self.cycle             = 0
        self.season            = None
        self.day               = 0

    # ── block access ────────────────────────────────────────────────────────

    def get_block(self, x, y, z):
        return self._overrides.get((x, y, z), self._fill)

    def set_block(self, x, y, z, block_id):
        if block_id == self._fill:
            self._overrides.pop((x, y, z), None)
        else:
            self._overrides[(x, y, z)] = block_id

        if block_id == GRASS:
            self.vegetation_ages.pop((x, z), None)
        elif block_id in _DEFAULT_VEGETATION_AGE:
            self.vegetation_ages.setdefault((x, z), _DEFAULT_VEGETATION_AGE[block_id])

    def fill(self, block_id):
        """Set every position to block_id and clear overrides."""
        self._fill = block_id
        self._overrides.clear()
        self.vegetation_ages.clear()

    # ── fauna / structures ──────────────────────────────────────────────────

    @staticmethod
    def _normalize_instance(raw, fields, required):
        """Coerce one saved instance into the expected field types. Returns
        None if any required identity/position field is missing or unusable."""
        inst = {}
        for field, cast in fields.items():
            if field not in raw:
                continue
            try:
                inst[field] = cast(raw[field])
            except (TypeError, ValueError):
                return None
        if not all(f in inst for f in required):
            return None
        return inst

    @classmethod
    def normalize_creature(cls, raw):
        return cls._normalize_instance(raw, _CREATURE_FIELDS, ('id', 'x', 'z'))

    @classmethod
    def normalize_structure(cls, raw):
        return cls._normalize_instance(raw, _STRUCTURE_FIELDS,
                                       ('id', 'type', 'x', 'z'))

    # ── helpers ─────────────────────────────────────────────────────────────

    def top_y(self, x, z):
        """Y of the topmost non-air block in column (x, z), or -1 if empty."""
        for y in range(self.size[1] - 1, -1, -1):
            if self.get_block(x, y, z) != AIR:
                return y
        return -1

    def overrides_at_y(self, y):
        """Return {(x, z): block_id} for every override at the given y-level."""
        return {(x, z): bid for (x, y2, z), bid in self._overrides.items() if y2 == y}

    def visible_surface(self):
        """Yield (x, y, z, block_id) for the topmost non-air block per column."""
        sx, sy, sz = self.size
        for x in range(sx):
            for z in range(sz):
                for y in range(sy - 1, -1, -1):
                    bid = self.get_block(x, y, z)
                    if bid != AIR:
                        yield x, y, z, bid
                        break

    # ── persistence ─────────────────────────────────────────────────────────

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {
                    "version": FORMAT_VERSION,
                    "size": list(self.size),
                    "moisture": self.moisture,
                    "fertility": self.fertility,
                    "fill": BLOCK_NAMES.get(self._fill, str(self._fill)),
                    "overrides": {
                        f"{x},{y},{z}": BLOCK_NAMES.get(bid, str(bid))
                        for (x, y, z), bid in self._overrides.items()
                    },
                    "vegetation_ages": {
                        f"{x},{z}": age
                        for (x, z), age in self.vegetation_ages.items()
                    },
                    "creatures": {
                        name: list(instances)
                        for name, instances in self.creatures.items()
                    },
                    "next_creature_id": self.next_creature_id,
                    "structures": list(self.structures),
                    "next_structure_id": self.next_structure_id,
                    "time": {
                        "cycle": self.cycle,
                        "season": self.season,
                        "day": self.day,
                    },
                },
                f, indent=2,
            )

    @classmethod
    def load(cls, path):
        with open(path) as f:
            raw = json.load(f)
        c = cls(size=tuple(raw["size"]))
        c.moisture  = raw.get("moisture", 0)
        c.fertility = raw.get("fertility", 0)
        c._fill = BLOCK_IDS.get(raw.get("fill", "air"), AIR)
        for key, name in raw.get("overrides", {}).items():
            x, y, z = map(int, key.split(","))
            c._overrides[(x, y, z)] = BLOCK_IDS.get(name, AIR)

        c.vegetation_ages = {}
        for key, age in raw.get("vegetation_ages", {}).items():
            x, z = map(int, key.split(","))
            c.vegetation_ages[(x, z)] = int(age)

        c.creatures = {}
        for name, instances in (raw.get("creatures") or {}).items():
            if not isinstance(instances, list):
                continue
            restored = [inst for inst in (
                c.normalize_creature(i) for i in instances if isinstance(i, dict)
            ) if inst is not None]
            c.creatures[str(name)] = restored

        highest_id = max(
            (inst["id"] for instances in c.creatures.values() for inst in instances),
            default=0,
        )
        c.next_creature_id = max(int(raw.get("next_creature_id", 0)), highest_id)

        saved_structures = raw.get("structures")
        c.structures = [inst for inst in (
            c.normalize_structure(s)
            for s in (saved_structures if isinstance(saved_structures, list) else [])
            if isinstance(s, dict)
        ) if inst is not None]
        highest_structure_id = max((s["id"] for s in c.structures), default=0)
        c.next_structure_id = max(int(raw.get("next_structure_id", 0)),
                                  highest_structure_id)

        saved_time = raw.get("time") or {}
        c.cycle  = _as_int(saved_time.get("cycle"), 0)
        c.day    = _as_int(saved_time.get("day"), 0)
        season   = saved_time.get("season")
        c.season = str(season) if season else None

        for x in range(c.size[0]):
            for z in range(c.size[2]):
                bid = c.get_block(x, c.size[1] - 1, z)
                if bid in _DEFAULT_VEGETATION_AGE:
                    c.vegetation_ages.setdefault((x, z), _DEFAULT_VEGETATION_AGE[bid])
        return c
