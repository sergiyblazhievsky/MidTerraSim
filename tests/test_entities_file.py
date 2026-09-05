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


def _matches_item(entry):
    return entry in ITEM_NAMES or any(entry in i.get("tags", [])
                                      for i in ENTITIES["items"])


def _plants_matching(entry):
    return [v for v in ENTITIES["vegetation"]
            if entry == v["name"] or entry in v.get("tags", [])]


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
    # _act_feed resolves the diet twice, once for drops and once for standing
    # plants, so an entry matching both would put the same word to work on two
    # jobs at once -- tagging a crop "food" would have every rat browsing the
    # fields as well as eating the vegetables off them.
    for entry in cdef["diet"]:
        assert not (_matches_item(entry) and _plants_matching(entry)), \
            f"{cdef['name']}: diet entry {entry!r} is ambiguous"


@pytest.mark.parametrize("cdef", ENTITIES["creatures"], ids=lambda c: c["name"])
def test_every_diet_and_forage_entry_names_something(cdef):
    # Entries matching nothing are dropped in silence, so a typo costs the
    # creature a whole food source with no error to show for it.
    for key in ("diet", "forage"):
        for entry in cdef.get(key, []):
            assert _matches_item(entry) or _plants_matching(entry), \
                f"{cdef['name']}: {key} entry {entry!r} names nothing"


@pytest.mark.parametrize("cdef", ENTITIES["creatures"], ids=lambda c: c["name"])
def test_no_plant_is_both_eaten_and_raided(cdef):
    # Eating wins when a plant sits in both lists, leaving the forage tier to
    # scan for something the diet tier already claimed.
    eaten = {v["block_id"] for e in cdef["diet"] for v in _plants_matching(e)}
    raided = {v["block_id"] for e in cdef.get("forage", [])
              for v in _plants_matching(e)}

    assert not eaten & raided, f"{cdef['name']}: same plant eaten and raided"


def test_the_plants_a_rat_raids_all_drop_food_it_eats():
    # Raiding only pays off in loot: the hit itself feeds nobody, so a plant
    # worth felling has to leave something on the rat's menu behind.
    rat = next(c for c in ENTITIES["creatures"] if c["name"] == "rat")
    food = {i["name"] for i in ENTITIES["items"] if "food" in i["tags"]}
    raided = [v for e in rat["forage"] for v in _plants_matching(e)]

    assert [v["name"] for v in raided] == ["flower", "carrot", "cabbage"]
    for vdef in raided:
        dropped = {entry["item"] for pool in _loot_pools(vdef) for entry in pool}
        assert dropped & food, f"{vdef['name']} drops nothing a rat eats"


# ── crops ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["carrot", "cabbage"])
def test_the_crop_is_declared_as_vegetation_and_as_a_food_item(name):
    vdef = next(v for v in ENTITIES["vegetation"] if v["name"] == name)
    idef = next(i for i in ENTITIES["items"] if i["name"] == name)

    assert vdef["tags"] == ["flora", "small", "crops"]
    assert idef["tags"] == ["raw", "food", "vegetable"]


@pytest.mark.parametrize("name,gain", [("carrot", 2), ("cabbage", 3)])
def test_the_vegetable_is_a_bigger_meal_than_plain_fare(name, gain):
    # Undeclared items fall back to the eater's flat hunger_per_food (1), so
    # only the vegetables need a value of their own.
    idef = next(i for i in ENTITIES["items"] if i["name"] == name)
    plain = [i["name"] for i in ENTITIES["items"]
             if "food" in i["tags"] and "hunger_gain" not in i]

    assert idef["hunger_gain"] == gain
    assert sorted(plain) == ["berry", "meat", "seed"]


def test_both_animals_eat_the_vegetables_but_only_the_rat_eats_meat():
    # "vegetable" tags what a herbivore will pick up off the ground; the rat's
    # broader "food" covers the same vegetables plus seeds, berries and meat.
    by_name = {c["name"]: c for c in ENTITIES["creatures"]}
    vegetables = {i["name"] for i in ENTITIES["items"] if "vegetable" in i["tags"]}

    assert vegetables == {"carrot", "cabbage"}
    assert "vegetable" in by_name["rabbit"]["diet"]
    assert "food" in by_name["rat"]["diet"]
    assert "meat" not in by_name["rabbit"]["diet"]


def test_both_animals_raid_the_crops_rather_than_grazing_them():
    # A crop is worth felling, not nibbling: the vegetable it leaves behind is
    # the meal, which is why "crops" sits in forage for both animals.
    for cdef in ENTITIES["creatures"]:
        assert "crops" in cdef["forage"], f"{cdef['name']} does not raid crops"
        assert "crops" not in cdef["diet"], f"{cdef['name']} grazes crops"


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
