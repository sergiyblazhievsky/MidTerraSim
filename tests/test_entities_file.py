"""Consistency checks on the *real* entities.json.

Every other test in the suite runs against the trimmed fixture set in
conftest.py, so nothing validates the file the server actually ships with. The
failure modes here are quiet ones: a mistyped texture path renders as a plain
brown quad (that's how a missing burrow texture reached the client), an
unknown item name in a loot table raises only when that plant happens to die,
and a block id chunk.py doesn't know about loads with the wrong age.
"""
import json
from pathlib import Path

import pytest

from chunk import GRASS, Chunk

ROOT = Path(__file__).resolve().parent.parent
ENTITIES = json.loads((ROOT / "entities.json").read_text(encoding="utf-8"))

SECTIONS = ("items", "vegetation", "structures", "creatures")
ITEM_NAMES = {i["name"] for i in ENTITIES["items"]}


def _all_defs():
    for section in SECTIONS:
        for edef in ENTITIES[section]:
            yield section, edef


def _textures(edef):
    paths = [edef.get("texture"), edef.get("sleep_texture")]
    paths += [stage.get("texture") for stage in edef.get("stages", [])]
    return [p for p in paths if p]


def _loot_pools(edef):
    yield edef.get("contains", [])
    for stage in edef.get("stages", []):
        yield stage.get("contains", [])


@pytest.mark.parametrize("section,edef", list(_all_defs()),
                         ids=lambda v: v["name"] if isinstance(v, dict) else v)
def test_every_referenced_texture_exists(section, edef):
    for path in _textures(edef):
        assert (ROOT / path).is_file(), f"{edef['name']}: missing {path}"


@pytest.mark.parametrize("section,edef", list(_all_defs()),
                         ids=lambda v: v["name"] if isinstance(v, dict) else v)
def test_loot_tables_only_reference_declared_items(section, edef):
    for pool in _loot_pools(edef):
        for entry in pool:
            assert entry["item"] in ITEM_NAMES, \
                f"{edef['name']} drops unknown item {entry['item']!r}"
            lo, hi = entry["count"]
            assert 0 <= lo <= hi, f"{edef['name']}: bad count range for {entry['item']}"


def test_vegetation_block_ids_are_unique():
    bids = [v["block_id"] for v in ENTITIES["vegetation"]]
    assert len(bids) == len(set(bids))


@pytest.mark.parametrize("vdef", ENTITIES["vegetation"], ids=lambda v: v["name"])
def test_chunk_defaults_the_declared_initial_age_for_every_block_id(vdef):
    # chunk.py keeps its own block-id -> default age table for backfilling
    # hand-edited or older save files; it has to agree with entities.json.
    c = Chunk(size=(3, 2, 3))
    c.fill(GRASS)

    c.set_block(0, 1, 0, vdef["block_id"])

    assert c.vegetation_ages[(0, 0)] == vdef["initial_age"]


@pytest.mark.parametrize("vdef", ENTITIES["vegetation"], ids=lambda v: v["name"])
def test_stages_are_ordered_by_ascending_max_age(vdef):
    # _get_stage takes the first stage whose max_age covers the age, so an
    # out-of-order list silently shadows later stages.
    ages = [s["max_age"] for s in vdef["stages"]]
    assert ages == sorted(ages)


@pytest.mark.parametrize("cdef", ENTITIES["creatures"], ids=lambda c: c["name"])
def test_no_diet_entry_matches_both_a_plant_and_an_item(cdef):
    # _act_feed hands the whole turn to the herbivore path as soon as *any*
    # diet entry resolves to vegetation, so an entry matching both a plant and
    # an item would silently kill the drop-eating path. Tagging a crop "food",
    # for instance, would turn every rat into a browser.
    for entry in cdef["diet"]:
        matches_item = entry in ITEM_NAMES or any(
            entry in i.get("tags", []) for i in ENTITIES["items"])
        matches_plant = any(entry == v["name"] or entry in v.get("tags", [])
                            for v in ENTITIES["vegetation"])
        assert not (matches_item and matches_plant), \
            f"{cdef['name']}: diet entry {entry!r} is ambiguous"


# ── crops ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["carrot", "cabbage"])
def test_the_crop_is_declared_as_vegetation_and_as_a_food_item(name):
    vdef = next(v for v in ENTITIES["vegetation"] if v["name"] == name)
    idef = next(i for i in ENTITIES["items"] if i["name"] == name)

    assert vdef["tags"] == ["flora", "small", "crops"]
    assert idef["tags"] == ["raw", "food"]


@pytest.mark.parametrize("name", ["carrot", "cabbage"])
def test_the_crop_drops_its_own_vegetable(name):
    vdef = next(v for v in ENTITIES["vegetation"] if v["name"] == name)

    assert [e["item"] for stage in vdef["stages"] for e in stage["contains"]] == [name]


@pytest.mark.parametrize("name", ["carrot", "cabbage"])
def test_the_crop_lives_about_one_season(name):
    # A season is config.json's season_length (10) cycles. Decay takes one age
    # every 4th cycle, and only when a moisture roll passes (~70% at the
    # seasonal average), so two ages run out after ~10 cycles -- see the
    # lifetime table in README.
    vdef = next(v for v in ENTITIES["vegetation"] if v["name"] == name)

    assert vdef["initial_age"] == 2
    assert vdef["age_decay_every_n_cycles"] == 4


@pytest.mark.parametrize("name", ["carrot", "cabbage"])
def test_the_crop_spawns_with_no_placement_restrictions(name):
    # The chance itself is a tuning knob and deliberately not pinned here.
    vdef = next(v for v in ENTITIES["vegetation"] if v["name"] == name)

    assert 0 < vdef["spawn"]["chance"] <= 1
    assert vdef["spawn"]["max_same_within"] is None
    assert [k for k in vdef["spawn"] if k.startswith("requires_no_")] == []


def test_both_crops_spawn_at_the_same_rate():
    chances = {v["spawn"]["chance"] for v in ENTITIES["vegetation"]
               if "crops" in v["tags"]}
    assert len(chances) == 1


def test_rats_will_eat_and_hoard_the_new_crops():
    # The rat's diet is the "food" tag, so tagging the vegetables raw/food is
    # what puts them on the menu -- and, via the stock need, into burrows.
    rat = next(c for c in ENTITIES["creatures"] if c["name"] == "rat")
    food = {i["name"] for i in ENTITIES["items"] if "food" in i["tags"]}

    assert rat["diet"] == ["food"]
    assert {"carrot", "cabbage"} <= food
