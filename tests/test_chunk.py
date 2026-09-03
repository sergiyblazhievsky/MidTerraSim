"""Unit tests for chunk.py's Chunk data model (block access, vegetation-age
bookkeeping, and JSON persistence)."""
import json

from chunk import AIR, BUSH, FLOWER, GRASS, GRASS_PATCH, TREE, Chunk


def test_default_fill_is_air():
    c = Chunk(size=(3, 2, 3))
    assert c.get_block(0, 0, 0) == AIR
    assert c.get_block(2, 1, 2) == AIR


def test_fill_sets_default_and_clears_overrides_and_ages():
    c = Chunk(size=(3, 2, 3))
    c.set_block(1, 1, 1, GRASS)
    c.vegetation_ages[(1, 1)] = 5

    c.fill(GRASS)

    assert c.get_block(0, 0, 0) == GRASS
    assert c.get_block(1, 1, 1) == GRASS  # prior override cleared, falls back to new fill
    assert c.vegetation_ages == {}


def test_set_block_flower_bush_tree_get_default_ages():
    c = Chunk(size=(3, 2, 3))
    c.fill(GRASS)

    c.set_block(0, 1, 0, FLOWER)
    c.set_block(1, 1, 0, BUSH)
    c.set_block(2, 1, 0, TREE)

    assert c.vegetation_ages[(0, 0)] == 2
    assert c.vegetation_ages[(1, 0)] == 5
    assert c.vegetation_ages[(2, 0)] == 10


def test_set_block_grass_patch_gets_default_age():
    c = Chunk(size=(3, 2, 3))
    c.fill(GRASS)

    c.set_block(0, 1, 0, GRASS_PATCH)

    assert c.vegetation_ages[(0, 0)] == 5
    assert c.get_block(0, 1, 0) == GRASS_PATCH


def test_set_block_to_grass_clears_vegetation_age():
    c = Chunk(size=(3, 2, 3))
    c.fill(GRASS)
    c.set_block(0, 1, 0, FLOWER)
    assert (0, 0) in c.vegetation_ages

    c.set_block(0, 1, 0, GRASS)

    assert (0, 0) not in c.vegetation_ages
    assert c.get_block(0, 1, 0) == GRASS


def test_set_block_does_not_overwrite_existing_vegetation_age():
    c = Chunk(size=(3, 2, 3))
    c.fill(GRASS)
    c.set_block(0, 1, 0, FLOWER)
    c.vegetation_ages[(0, 0)] = 1  # simulate an already-aged flower

    c.set_block(0, 1, 0, FLOWER)  # re-applying the same block type...

    assert c.vegetation_ages[(0, 0)] == 1  # ...must not reset the age (setdefault)


def test_set_block_to_fill_value_clears_override_entry():
    c = Chunk(size=(2, 2, 2))
    c.fill(GRASS)
    c.set_block(0, 0, 0, FLOWER)
    assert c.get_block(0, 0, 0) == FLOWER

    c.set_block(0, 0, 0, GRASS)  # GRASS is the current fill value

    assert c.get_block(0, 0, 0) == GRASS
    assert (0, 0, 0) not in c._overrides


def test_top_y_finds_topmost_non_air_or_minus_one_when_empty():
    c = Chunk(size=(2, 5, 2))
    assert c.top_y(0, 0) == -1

    c.set_block(0, 2, 0, GRASS)
    assert c.top_y(0, 0) == 2

    c.set_block(0, 4, 0, GRASS)
    assert c.top_y(0, 0) == 4


def test_overrides_at_y_filters_by_y_level():
    c = Chunk(size=(3, 3, 3))
    c.set_block(0, 1, 0, GRASS)
    c.set_block(1, 1, 1, FLOWER)
    c.set_block(2, 2, 2, TREE)

    result = c.overrides_at_y(1)

    assert result == {(0, 0): GRASS, (1, 1): FLOWER}
    assert (2, 2) not in result


def test_visible_surface_yields_topmost_block_per_column():
    c = Chunk(size=(2, 3, 1))
    c.set_block(0, 0, 0, GRASS)
    c.set_block(0, 2, 0, FLOWER)  # taller than the GRASS below in the same column
    c.set_block(1, 1, 0, TREE)

    surface = list(c.visible_surface())

    assert (0, 2, 0, FLOWER) in surface
    assert (1, 1, 0, TREE) in surface
    assert len(surface) == 2  # one entry per (x, z) column


def test_save_and_load_round_trip(tmp_path):
    c = Chunk(size=(4, 2, 4), moisture=55, fertility=33)
    c.fill(GRASS)
    c.set_block(1, 1, 1, FLOWER)
    c.set_block(2, 1, 2, TREE)
    c.vegetation_ages[(1, 1)] = 1  # override the default age before saving

    path = tmp_path / "chunks" / "test.wrld"
    c.save(str(path))
    assert path.exists()

    loaded = Chunk.load(str(path))

    assert loaded.size == (4, 2, 4)
    assert loaded.moisture == 55
    assert loaded.fertility == 33
    assert loaded.get_block(1, 1, 1) == FLOWER
    assert loaded.get_block(2, 1, 2) == TREE
    assert loaded.vegetation_ages[(1, 1)] == 1
    assert loaded.vegetation_ages[(2, 2)] == 10  # default backfilled for a fresh tree


def test_save_writes_valid_json_with_expected_top_level_keys(tmp_path):
    c = Chunk(size=(2, 2, 2))
    c.fill(GRASS)
    path = tmp_path / "chunks" / "test.wrld"

    c.save(str(path))

    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert raw["size"] == [2, 2, 2]
    assert raw["fill"] == "grass"
    for key in ("overrides", "vegetation_ages", "moisture", "fertility"):
        assert key in raw


def test_load_backfills_missing_vegetation_age_for_surface_blocks(tmp_path):
    # Simulates loading a legacy save that predates vegetation_ages tracking.
    raw = {
        "version": 1,
        "size": [2, 2, 2],
        "fill": "air",
        "overrides": {"0,1,0": "flower"},
    }
    path = tmp_path / "legacy.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.vegetation_ages[(0, 0)] == 2


def test_load_backfills_missing_vegetation_age_for_grass_patch(tmp_path):
    raw = {
        "version": 1,
        "size": [2, 2, 2],
        "fill": "air",
        "overrides": {"0,1,0": "grass_patch"},
    }
    path = tmp_path / "legacy_grass.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.get_block(0, 1, 0) == GRASS_PATCH
    assert loaded.vegetation_ages[(0, 0)] == 5
