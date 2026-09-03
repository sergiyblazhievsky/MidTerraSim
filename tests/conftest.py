"""Shared fixtures for the MidTerraSim test suite.

These tests exercise ``server.py``'s ``World`` class and HTTP handlers, and
``chunk.py``'s ``Chunk`` data model, without touching the real project files
(``config.json``, ``entities.json``, ``chunks/chunk_0_0.wrld``).

``server.py`` binds its file paths as *default parameter values* on
``load_config``/``load_entities`` (evaluated once, at import time), and reads
``WORLD_FILE``/``CONFIG_PATH``/``ENTITIES_PATH`` as bare module globals
elsewhere. To fully redirect a fresh ``World()`` at isolated fixture files we
therefore need to monkeypatch *both*:

  * the module-level path constants (for direct global lookups such as
    ``Chunk.load(WORLD_FILE)`` and ``CONFIG_PATH.stat()``), and
  * the loader functions themselves (via ``functools.partial``, so calls made
    with no arguments -- e.g. ``self.config = load_config()`` inside
    ``World.__init__`` -- resolve to the fixture path instead of the frozen
    default).
"""
import functools
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server as server_module  # noqa: E402
from chunk import BUSH, FLOWER, GRASS, GRASS_PATCH, TREE, Chunk  # noqa: E402


# A small, deterministic entities.json used by every test. Kept structurally
# compatible with the real entities.json (same keys/shape) but with tightened
# random ranges (e.g. guaranteed [1, 1] drop counts) so tests can assert exact
# outcomes without needing to patch `random` for every scenario.
TEST_ENTITIES = {
    "items": [
        {"name": "seed", "tags": ["raw", "food"], "texture": "textures/seed_16.png"},
        {"name": "berry", "tags": ["raw", "food"], "texture": "textures/berry_16.png"},
        {"name": "meat", "tags": ["raw", "food"], "texture": "textures/meat_16.png"},
        {"name": "log", "tags": ["material"], "texture": "textures/log_16.png"},
        {"name": "stick", "tags": ["material"], "texture": "textures/stick_16.png"},
    ],
    "vegetation": [
        {
            "name": "flower",
            "tags": ["flora", "small", "flower"],
            "block_id": FLOWER,
            "initial_age": 2,
            "age_decay_every_n_cycles": 1,
            "contains": [],
            "stages": [
                {
                    "max_age": 1,
                    "texture": "textures/flower_dry.png",
                    "height": 1.0,
                    "contains": [{"item": "seed", "count": [1, 1]}],
                },
                {
                    "max_age": 99,
                    "texture": "textures/flower.png",
                    "height": 1.0,
                    "contains": [{"item": "seed", "count": [0, 1]}],
                },
            ],
            "spawn": {
                "chance": 0.090,
                "max_same_within": {"radius": 1, "count": 2},
            },
        },
        {
            "name": "bush",
            "tags": ["flora", "medium", "bush"],
            "block_id": BUSH,
            "initial_age": 5,
            "age_decay_every_n_cycles": 2,
            "contains": [],
            "stages": [
                {
                    "max_age": 1,
                    "texture": "textures/bush_dry.png",
                    "height": 2.0,
                    "contains": [{"item": "berry", "count": [1, 1]}],
                },
                {
                    "max_age": 99,
                    "texture": "textures/bush.png",
                    "height": 1.0,
                    "contains": [],
                },
            ],
            "spawn": {
                "chance": 0.030,
                "requires_no_tree_within": 1,
                "requires_no_bush_within": 1,
                "max_same_within": None,
            },
        },
        {
            "name": "tree",
            "tags": ["flora", "large", "tree"],
            "block_id": TREE,
            "initial_age": 10,
            "age_decay_every_n_cycles": 2,
            "contains": [],
            "stages": [
                {
                    "max_age": 1,
                    "texture": "textures/tree_dead.png",
                    "height": 1.0,
                    "contains": [{"item": "log", "count": [1, 1]}],
                },
                {
                    "max_age": 99,
                    "texture": "textures/tree.png",
                    "height": 4.0,
                    "contains": [],
                },
            ],
            "spawn": {
                "chance": 0.010,
                "requires_no_tree_within": 2,
                "requires_no_bush_within": 1,
                "max_same_within": None,
            },
        },
        {
            "name": "grass",
            "tags": ["flora", "ground_cover"],
            "block_id": GRASS_PATCH,
            "initial_age": 5,
            "age_decay_every_n_cycles": 3,
            "contains": [],
            "stages": [
                {"max_age": 99, "texture": "textures/grass_xcross.png", "height": 0.3,
                 "width": 1.0, "contains": []},
            ],
            "spawn": {
                "chance": 0.150,
                "active_seasons": ["spring", "summer"],
            },
        },
    ],
    "creatures": [
        {
            "name": "rat",
            "tags": ["fauna", "small"],
            "texture": "textures/rat.png",
            "sleep_texture": "textures/rat_sleeping.png",
            "count": 2,
            "scale": [1.0, 1.0],
            "y_offset": 1.0,
            "move_interval_day": 3.0,
            "moves_at_night": False,
            "avoids_block_tag": "tree",
            "min_spawn_distance": 1,
            "reproduce_count": [1, 1],
            "initial_age": 2,
            "attack": 5,
            "initial_hunger": 3,
            "sleep_gain": 0.5,
            "needs": ["feed", "sleep"],
            "diet": ["food"],
            "hunger_per_food": 1,
            "contains": [{"item": "meat", "count": [1, 1]}],
        }
    ],
}

TEST_CONFIG = {
    "cycle_length": 10.0,
    "season_length": 2,
    "day_night_cycle": 60.0,
    "drop_lifetime": 5.0,
    "seasons": {
        "spring": {"moisture": 40, "fertility": 20, "texture": "soil.png"},
        "summer": {"moisture": 20, "fertility": 30, "texture": "soil.png"},
        "fall": {"moisture": 30, "fertility": 40, "texture": "soil_fall.png"},
        "winter": {"moisture": 30, "fertility": 10, "texture": "soil_winter.png"},
    },
    "server": {"host": "127.0.0.1", "port": 8765, "tick_rate": 20.0, "save_interval": 120.0},
    "client": {"host": "127.0.0.1", "port": 8765, "poll_interval": 0.15, "request_timeout": 2.0},
}


def build_test_chunk(size=(6, 2, 6), moisture=40, fertility=20):
    """A small, flat, all-grass chunk -- big enough to exercise radius-based
    neighbor checks (up to radius 2) without the cost of a full 100x100 world."""
    c = Chunk(size=size, moisture=moisture, fertility=fertility)
    c.fill(GRASS)
    return c


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect server.py's file-backed globals at an isolated tmp_path
    sandbox and write minimal config/entities/world fixtures there. Returns
    the fixture paths so individual tests can inspect/mutate them further."""
    config_path = tmp_path / "config.json"
    entities_path = tmp_path / "entities.json"
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    world_path = chunks_dir / "chunk_0_0.wrld"

    config_path.write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
    entities_path.write_text(json.dumps(TEST_ENTITIES), encoding="utf-8")
    build_test_chunk().save(str(world_path))

    monkeypatch.setattr(server_module, "WORLD_FILE", str(world_path))
    monkeypatch.setattr(server_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server_module, "ENTITIES_PATH", entities_path)
    monkeypatch.setattr(
        server_module, "load_config",
        functools.partial(server_module.load_config, path=config_path),
    )
    monkeypatch.setattr(
        server_module, "load_entities",
        functools.partial(server_module.load_entities, path=entities_path),
    )

    return {
        "config_path": config_path,
        "entities_path": entities_path,
        "world_path": world_path,
    }


@pytest.fixture
def world(isolated_paths):
    """A freshly constructed World over the isolated fixture files."""
    return server_module.World()


def isolate_creature(world, ci, i):
    """Put every OTHER seeded creature of type `ci` to sleep so it can never
    interact (pickup/attack/etc.) regardless of its randomly-seeded position.

    `World._seed_creatures()` places creatures at genuinely random positions
    (subject only to a `min_spawn_distance` spacing hint), so a test that
    configures a single creature's position/state but leaves sibling
    instances untouched can flake if one of them happens to land adjacent to
    whatever the test is probing. Call this before exercising interaction
    logic (e.g. `_update_drops`) to make the "one relevant creature" scenario
    deterministic irrespective of that seeding randomness.
    """
    for other_i, st in enumerate(world.all_creature_stats[ci]):
        if other_i != i:
            st["asleep"] = True
