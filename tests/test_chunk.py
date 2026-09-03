"""Unit tests for chunk.py's Chunk data model (block access, vegetation-age
bookkeeping, and JSON persistence)."""
import json

import pytest

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
    assert raw["version"] == 2
    assert raw["size"] == [2, 2, 2]
    assert raw["fill"] == "grass"
    for key in ("overrides", "vegetation_ages", "moisture", "fertility",
                "creatures", "next_creature_id"):
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


# ── fauna persistence ────────────────────────────────────────────────────────

def test_creatures_survive_a_save_load_round_trip(tmp_path):
    c = Chunk(size=(4, 2, 4))
    c.fill(GRASS)
    c.creatures = {
        "rat": [{"id": 1, "x": 1, "z": 2, "age": 2, "hunger": 3,
                 "attack": 1, "sleep": 0.5, "asleep": True}],
        "rabbit": [{"id": 2, "x": 3, "z": 0, "age": 3, "hunger": 5,
                    "attack": 1, "sleep": 0.0, "asleep": False}],
    }
    c.next_creature_id = 7
    path = tmp_path / "fauna.wrld"

    c.save(str(path))
    loaded = Chunk.load(str(path))

    assert loaded.creatures["rat"] == c.creatures["rat"]
    assert loaded.creatures["rabbit"] == c.creatures["rabbit"]
    assert loaded.next_creature_id == 7


def test_load_leaves_creatures_empty_for_a_version_1_file(tmp_path):
    # A pre-fauna save must be distinguishable from "every creature died",
    # so the server knows to seed rather than restore an empty world.
    raw = {"version": 1, "size": [2, 2, 2], "fill": "grass", "overrides": {}}
    path = tmp_path / "legacy.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.creatures == {}
    assert loaded.next_creature_id == 0


def test_load_keeps_an_explicitly_empty_creature_list(tmp_path):
    raw = {"version": 2, "size": [2, 2, 2], "fill": "grass", "overrides": {},
           "creatures": {"rat": []}, "next_creature_id": 4}
    path = tmp_path / "extinct.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.creatures == {"rat": []}
    assert loaded.next_creature_id == 4


def test_load_coerces_creature_field_types_and_drops_broken_entries(tmp_path):
    raw = {
        "version": 2, "size": [4, 2, 4], "fill": "grass", "overrides": {},
        "creatures": {"rat": [
            {"id": "5", "x": "1", "z": 2, "age": "2", "hunger": 3,
             "attack": 1, "sleep": "0.5", "asleep": True},
            {"id": 6, "x": "not-a-number", "z": 0},   # unusable position
            {"x": 1, "z": 1},                          # no id
            "nonsense",
        ]},
    }
    path = tmp_path / "messy.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.creatures["rat"] == [
        {"id": 5, "x": 1, "z": 2, "age": 2, "hunger": 3,
         "attack": 1, "sleep": 0.5, "asleep": True}
    ]


def test_structures_survive_a_save_load_round_trip(tmp_path):
    c = Chunk(size=(4, 2, 4))
    c.fill(GRASS)
    c.structures = [
        {"id": 1, "type": "burrow", "x": 2, "z": 3, "age": 2, "contains": []},
    ]
    c.next_structure_id = 4
    path = tmp_path / "structures.wrld"

    c.save(str(path))
    loaded = Chunk.load(str(path))

    assert loaded.structures == c.structures
    assert loaded.next_structure_id == 4


def test_load_leaves_structures_empty_for_a_file_without_them(tmp_path):
    raw = {"version": 1, "size": [2, 2, 2], "fill": "grass", "overrides": {}}
    path = tmp_path / "no_structures.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.structures == []
    assert loaded.next_structure_id == 0


def test_load_drops_structures_missing_an_id_type_or_position(tmp_path):
    raw = {"version": 2, "size": [4, 2, 4], "fill": "grass", "overrides": {},
           "structures": [
               {"id": 1, "type": "burrow", "x": 1, "z": 1, "age": 2, "contains": []},
               {"id": 2, "x": 1, "z": 2},                       # no type
               {"id": 3, "type": "burrow", "z": 2},             # no x
               {"type": "burrow", "x": 0, "z": 0},              # no id
               {"id": 5, "type": "burrow", "x": 1, "z": 1, "contains": "nope"},
               "junk",
           ]}
    path = tmp_path / "messy_structures.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert [s["id"] for s in loaded.structures] == [1]


def test_load_derives_next_structure_id_from_the_highest_saved_id(tmp_path):
    raw = {"version": 2, "size": [2, 2, 2], "fill": "grass", "overrides": {},
           "structures": [{"id": 8, "type": "burrow", "x": 0, "z": 0, "age": 1}],
           "next_structure_id": 3}
    path = tmp_path / "stale_structure_id.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert Chunk.load(str(path)).next_structure_id == 8


def test_a_creature_home_round_trips_including_null(tmp_path):
    c = Chunk(size=(2, 2, 2))
    c.fill(GRASS)
    c.creatures = {"rat": [
        {"id": 1, "x": 0, "z": 0, "home": 4, "home_need": 0.0},
        {"id": 2, "x": 1, "z": 1, "home": None, "home_need": 1.5},
    ]}
    path = tmp_path / "homes.wrld"

    c.save(str(path))
    loaded = Chunk.load(str(path))

    assert loaded.creatures["rat"][0]["home"] == 4
    assert loaded.creatures["rat"][1]["home"] is None
    assert loaded.creatures["rat"][1]["home_need"] == 1.5


@pytest.mark.parametrize("value", [None, ""])
def test_an_empty_home_is_read_as_no_home_rather_than_dropping_the_creature(
    tmp_path, value
):
    raw = {"version": 2, "size": [2, 2, 2], "fill": "grass", "overrides": {},
           "creatures": {"rat": [{"id": 1, "x": 0, "z": 0, "home": value}]}}
    path = tmp_path / "empty_home.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert len(loaded.creatures["rat"]) == 1
    assert loaded.creatures["rat"][0]["home"] is None


def test_load_defaults_the_clock_when_the_time_section_is_absent(tmp_path):
    raw = {"version": 1, "size": [2, 2, 2], "fill": "grass", "overrides": {}}
    path = tmp_path / "legacy_clock.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert (loaded.cycle, loaded.season, loaded.day) == (0, None, 0)


def test_clock_survives_a_save_load_round_trip(tmp_path):
    c = Chunk(size=(2, 2, 2))
    c.fill(GRASS)
    c.cycle, c.season, c.day = 41, "fall", 6
    path = tmp_path / "clock.wrld"

    c.save(str(path))
    loaded = Chunk.load(str(path))

    assert (loaded.cycle, loaded.season, loaded.day) == (41, "fall", 6)


def test_load_tolerates_a_malformed_clock(tmp_path):
    raw = {"version": 2, "size": [2, 2, 2], "fill": "grass", "overrides": {},
           "time": {"cycle": "nope", "season": "", "day": None}}
    path = tmp_path / "bad_clock.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert (loaded.cycle, loaded.season, loaded.day) == (0, None, 0)


def test_load_skips_a_creature_entry_that_is_not_a_list(tmp_path):
    raw = {"version": 2, "size": [2, 2, 2], "fill": "grass", "overrides": {},
           "creatures": {"rat": "oops", "rabbit": [{"id": 1, "x": 0, "z": 0}]}}
    path = tmp_path / "malformed.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert "rat" not in loaded.creatures
    assert len(loaded.creatures["rabbit"]) == 1


def test_load_derives_next_creature_id_from_the_highest_saved_id(tmp_path):
    raw = {
        "version": 2, "size": [2, 2, 2], "fill": "grass", "overrides": {},
        "creatures": {"rat": [{"id": 9, "x": 0, "z": 0}]},
        "next_creature_id": 2,   # stale/lower than a live id
    }
    path = tmp_path / "stale_id.wrld"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = Chunk.load(str(path))

    assert loaded.next_creature_id == 9
