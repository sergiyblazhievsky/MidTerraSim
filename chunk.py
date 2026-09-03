"""
Chunk API — import this from external editors/tools too.

JSON format:
  {
    "version": 1,
    "size": [100, 100, 100],   # X, Y, Z dimensions
    "fill": "grass",            # default block for every unspecified position
    "overrides": {              # sparse per-block overrides, key = "x,y,z"
      "50,99,50": "air"
    }
  }
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


class Chunk:
    def __init__(self, size=(100, 100, 100), moisture=0, fertility=0):
        self.size             = tuple(size)
        self.moisture         = moisture   # 0-100
        self.fertility        = fertility  # 0-100
        self._fill            = AIR
        self._overrides       = {}   # {(x, y, z): block_id}
        self.vegetation_ages  = {}  # {(x, z): age}

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
                    "version": 1,
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

        for x in range(c.size[0]):
            for z in range(c.size[2]):
                bid = c.get_block(x, c.size[1] - 1, z)
                if bid in _DEFAULT_VEGETATION_AGE:
                    c.vegetation_ages.setdefault((x, z), _DEFAULT_VEGETATION_AGE[bid])
        return c
