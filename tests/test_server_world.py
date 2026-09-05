"""Unit tests for server.py's World class -- the authoritative simulation
engine (config loading, seeding, ecology rules, creature AI/needs, daily and
seasonal lifecycle, the flora simulation cycle, tick() scheduling, and the
renderable snapshot)."""
import json
import os
import time as time_module

import pytest

import server as server_module
from chunk import BUSH, CABBAGE, CARROT, FLOWER, GRASS, GRASS_PATCH, TREE

from tests.conftest import isolate_creature


# ── config loading ───────────────────────────────────────────────────────────

def test_load_config_merges_test_overrides(isolated_paths):
    cfg = server_module.load_config()
    assert cfg["cycle_length"] == 10.0
    assert cfg["season_length"] == 2
    assert cfg["seasons"]["spring"]["fertility"] == 20
    assert cfg["server"]["tick_rate"] == 20.0


def test_load_config_fills_in_missing_season_fields(tmp_path):
    # Overriding only one season's one field must still backfill every other
    # field (moisture, texture) and every other season from the defaults.
    partial_cfg = {"seasons": {"spring": {"fertility": 99}}}
    path = tmp_path / "partial_config.json"
    path.write_text(json.dumps(partial_cfg), encoding="utf-8")

    cfg = server_module.load_config(path=path)

    assert cfg["seasons"]["spring"]["fertility"] == 99
    assert cfg["seasons"]["spring"]["moisture"] == server_module.DEFAULT_CONFIG["seasons"]["spring"]["moisture"]
    assert "winter" in cfg["seasons"]


def test_load_config_creates_file_if_missing(tmp_path):
    path = tmp_path / "auto_config.json"
    assert not path.exists()

    cfg = server_module.load_config(path=path)

    assert path.exists()
    assert cfg["cycle_length"] == server_module.DEFAULT_CONFIG["cycle_length"]


# ── World construction / seeding ─────────────────────────────────────────────

def test_world_exits_if_world_file_missing(isolated_paths):
    isolated_paths["world_path"].unlink()
    with pytest.raises(SystemExit):
        server_module.World()


def test_world_loads_chunk_and_seeds_creatures(world):
    assert world.sx == 6 and world.sz == 6
    assert world.SY == world.sy - 1
    # fixture entities.json defines rat + rabbit, each with count=2
    assert len(world.all_creature_positions) == 2
    assert len(world.all_creature_positions[0]) == 2
    assert len(world.all_creature_positions[1]) == 2
    assert len(world.all_creature_stats[0]) == 2
    assert world.creature_defs[0]["name"] == "rat"
    assert world.creature_defs[1]["name"] == "rabbit"
    ids = {st["id"] for stats in world.all_creature_stats for st in stats}
    assert len(ids) == 4  # unique ids across both types


def test_seeded_creature_stats_use_entity_defaults(world):
    st = world.all_creature_stats[0][0]
    assert st["age"] == 2
    assert st["hunger"] == 3
    assert st["attack"] == 5
    assert st["sleep"] == 0.0
    assert st["asleep"] is False


def test_seeded_rabbit_stats_use_entity_defaults(world):
    st = world.all_creature_stats[1][0]
    assert st["age"] == 3
    assert st["hunger"] == 5
    assert st["attack"] == 1


# ── structures: home need and burrow building ────────────────────────────────

def test_creatures_start_homeless_with_no_accrued_need(world):
    for stats in world.all_creature_stats:
        for st in stats:
            assert st["home"] is None
            assert st["home_need"] == 0.0
    assert world.world_structures == []


def test_resolve_home_structure_matches_a_declared_dweller(world):
    for cdef in world.creature_defs:
        assert world._resolve_home_structure(cdef)["name"] == "burrow"


def test_resolve_home_structure_matches_a_dweller_by_tag(world):
    world.structure_defs[0]["dwellers"] = ["small"]  # rat/rabbit tag, not name
    world.structure_defs_by_name["burrow"]["dwellers"] = ["small"]

    assert world._resolve_home_structure(world.creature_defs[0])["name"] == "burrow"


def test_resolve_home_structure_none_when_creature_is_not_a_dweller(world):
    world.structure_defs[0]["dwellers"] = ["badger"]

    assert world._resolve_home_structure(world.creature_defs[0]) is None


def test_home_need_accrues_by_home_gain_each_day_start(world):
    ci = 0
    st = world.all_creature_stats[ci][0]

    world._on_day_start()
    assert st["home_need"] == 0.5
    world._on_day_start()
    assert st["home_need"] == 1.0


def test_home_need_stops_accruing_once_the_creature_has_a_home(world):
    ci = 0
    st = world.all_creature_stats[ci][0]
    st["home"] = 1

    world._on_day_start()

    assert st["home_need"] == 0.0


def test_computed_home_need_is_zero_while_housed(world):
    cdef = world.creature_defs[0]
    st = world.all_creature_stats[0][0]
    st["home_need"] = 3.0

    assert world._compute_creature_needs(cdef, st)["home"] == 3.0
    st["home"] = 7
    assert world._compute_creature_needs(cdef, st)["home"] == 0


def test_act_home_builds_a_burrow_on_the_current_tile(world):
    ci = 0
    cdef = world.creature_defs[ci]
    before_rev = world.structure_revision

    result = world._act_home(ci, 0, cdef, 2, 3, avoids=set())

    assert result == (2, 3)  # stays put to build
    assert len(world.world_structures) == 1
    burrow = world.world_structures[0]
    assert (burrow["type"], burrow["x"], burrow["z"]) == ("burrow", 2, 3)
    assert burrow["age"] == 2  # initial_age from entities.json
    assert burrow["contains"] == []
    assert world.structure_revision == before_rev + 1


def test_act_home_records_the_burrow_id_as_the_creatures_home(world):
    ci = 0
    st = world.all_creature_stats[ci][0]
    st["home_need"] = 2.0

    world._act_home(ci, 0, world.creature_defs[ci], 1, 1, avoids=set())

    assert st["home"] == world.world_structures[0]["id"]
    assert st["home_need"] == 0.0  # need satisfied


def test_creature_move_acts_on_home_when_it_is_the_strongest_need(world):
    ci = 0
    st = world.all_creature_stats[ci][0]
    st["hunger"] = world.creature_defs[ci]["initial_hunger"]  # feed need 0
    st["sleep"] = 0.0
    st["home_need"] = 1.0

    result = world._creature_move(ci, 0, world.creature_defs[ci], 4, 4, avoids=set())

    assert result == (4, 4)
    assert len(world.world_structures) == 1
    assert st["home"] == world.world_structures[0]["id"]


def test_act_home_adopts_an_existing_burrow_instead_of_building_a_second(world):
    # Only one structure may occupy a tile.
    ci = 0
    cdef = world.creature_defs[ci]
    world._act_home(ci, 0, cdef, 2, 2, avoids=set())
    existing_id = world.world_structures[0]["id"]

    result = world._act_home(ci, 1, cdef, 2, 2, avoids=set())

    assert result == (2, 2)
    assert len(world.world_structures) == 1
    assert world.all_creature_stats[ci][1]["home"] == existing_id


def test_act_home_wanders_when_the_tile_holds_a_structure_it_cannot_dwell_in(world):
    # The rat has a home type available (nest) but is standing on a burrow
    # that only rabbits may use, and the tile can't hold both.
    ci = 0
    cdef = world.creature_defs[ci]
    world.structure_defs[0]["dwellers"] = ["rabbit"]
    nest = {"name": "nest", "texture": "textures/nest.png", "render": "surface",
            "initial_age": 2, "break_chance": 0.2, "dwellers": ["rat"],
            "contains": []}
    world.structure_defs.append(nest)
    world.structure_defs_by_name["nest"] = nest
    world._build_structure(world.structure_defs[0], 2, 2)   # a rabbits-only burrow

    result = world._act_home(ci, 0, cdef, 2, 2, avoids=set())

    assert result != (2, 2)                       # walks off to build elsewhere
    assert len(world.world_structures) == 1       # no second structure on the tile
    assert world.all_creature_stats[ci][0]["home"] is None


def test_act_feed_plants_wanders_when_no_plant_in_its_diet_exists(world):
    ci = 1  # rabbit: diet is grass then bush, and the fixture world has neither
    world.all_creature_stats[ci][0]["hunger"] = 0

    result = world._act_feed(ci, 0, world.creature_defs[ci], 2, 2, avoids=set())

    assert abs(result[0] - 2) + abs(result[1] - 2) == 1


def test_act_home_wanders_when_no_structure_accepts_the_creature(world):
    ci = 0
    world.structure_defs.clear()
    world.structure_defs_by_name.clear()

    result = world._act_home(ci, 0, world.creature_defs[ci], 2, 2, avoids=set())

    assert result != (2, 2)
    assert world.world_structures == []


def test_two_creatures_can_share_one_burrow_as_home(world):
    ci = 0
    cdef = world.creature_defs[ci]
    world._act_home(ci, 0, cdef, 1, 1, avoids=set())
    burrow_id = world.world_structures[0]["id"]

    world._act_home(ci, 1, cdef, 1, 1, avoids=set())

    homes = [st["home"] for st in world.all_creature_stats[ci][:2]]
    assert homes == [burrow_id, burrow_id]


def test_built_burrows_get_unique_increasing_ids(world):
    cdef = world.creature_defs[0]
    world._act_home(0, 0, cdef, 1, 1, avoids=set())
    world._act_home(0, 1, cdef, 3, 3, avoids=set())

    ids = [s["id"] for s in world.world_structures]
    assert ids[1] > ids[0]
    assert len(set(ids)) == 2


# ── stocking the larder ──────────────────────────────────────────────────────

def _housed_rat(world, x, z, home_x=None, home_z=None):
    """A fed, awake rat at (x, z) with a burrow, i.e. one that will stock."""
    ci = 0
    isolate_creature(world, ci, 0)
    world.all_creature_positions[ci][0] = (x, z)
    st = world.all_creature_stats[ci][0]
    st["hunger"] = world.creature_defs[ci]["initial_hunger"]   # feed need 0
    st["sleep"] = 0.0
    st["asleep"] = False
    burrow = world._build_structure(world.structure_defs[0],
                                    home_x if home_x is not None else x,
                                    home_z if home_z is not None else z)
    st["home"] = burrow["id"]
    st["home_need"] = 0.0
    return ci, st, burrow


def test_stock_need_is_the_constant_from_the_definition(world):
    cdef = world.creature_defs[0]
    st = world.all_creature_stats[0][0]

    assert world._compute_creature_needs(cdef, st)["stock"] == 0.9


def test_stock_need_stays_constant_regardless_of_state(world):
    cdef = world.creature_defs[0]
    st = world.all_creature_stats[0][0]
    st["hunger"] = 0
    st["home"] = 4

    assert world._compute_creature_needs(cdef, st)["stock"] == 0.9


def test_stock_need_falls_back_to_the_default_for_an_unusable_value(world):
    assert world._stock_need({"stock_need": "soon"}) == server_module.DEFAULT_STOCK_NEED
    assert world._stock_need({}) == server_module.DEFAULT_STOCK_NEED


def test_rat_and_rabbit_both_declare_the_stock_need(world):
    for cdef in world.creature_defs:
        assert "stock" in cdef["needs"]
        assert cdef["stock_need"] == 0.9


def test_hunger_outranks_stocking(world):
    # feed is >= 1 whenever the creature is at all hungry, stock is 0.9.
    cdef = world.creature_defs[0]
    st = world.all_creature_stats[0][0]
    st["hunger"] = cdef["initial_hunger"] - 1

    assert world._ranked_needs(world._compute_creature_needs(cdef, st))[0] == "feed"


def test_stocking_outranks_a_home_want_that_has_only_just_started(world):
    cdef = world.creature_defs[0]
    st = world.all_creature_stats[0][0]
    st["hunger"] = cdef["initial_hunger"]
    st["home_need"] = 0.5     # one day homeless

    assert world._ranked_needs(world._compute_creature_needs(cdef, st))[0] == "stock"


def test_a_long_homeless_creature_prefers_digging_to_stocking(world):
    cdef = world.creature_defs[0]
    st = world.all_creature_stats[0][0]
    st["hunger"] = cdef["initial_hunger"]
    st["home_need"] = 1.0     # two days homeless

    assert world._ranked_needs(world._compute_creature_needs(cdef, st))[0] == "home"


def test_act_stock_picks_up_a_drop_on_the_current_tile(world):
    ci, st, _ = _housed_rat(world, 5, 5)
    world._spawn_drop("berry", 2, 5, 5)

    result = world._act_stock(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    assert result == (5, 5)              # stays put to pick up
    assert st["carrying"] == "berry"
    assert world.world_drops[0]["count"] == 1   # took exactly one


def test_picking_up_the_last_of_a_drop_clears_it_from_the_world(world):
    ci, st, _ = _housed_rat(world, 5, 5)
    world._spawn_drop("berry", 1, 5, 5)

    world._act_stock(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    assert st["carrying"] == "berry"
    assert world.world_drops == []


def test_act_stock_steps_toward_a_drop_within_feed_radius(world):
    ci, st, _ = _housed_rat(world, 1, 1)
    world._spawn_drop("berry", 1, 4, 1)   # 3 tiles away, feed_radius is 5

    result = world._act_stock(ci, 0, world.creature_defs[ci], 1, 1, avoids=set())

    assert result == (2, 1)               # closed the distance
    assert st["carrying"] is None         # nothing picked up yet


def test_act_stock_declines_when_the_nearest_drop_is_out_of_range(world):
    ci, st, _ = _housed_rat(world, 0, 0)
    world._spawn_drop("berry", 1, 5, 5)   # 10 tiles away, feed_radius is 5

    assert world._act_stock(ci, 0, world.creature_defs[ci], 0, 0, avoids=set()) is None
    assert st["carrying"] is None


def test_act_stock_declines_when_nothing_edible_is_around(world):
    ci, st, _ = _housed_rat(world, 5, 5)
    world._spawn_drop("log", 1, 5, 5)     # not in the rat's food diet

    assert world._act_stock(ci, 0, world.creature_defs[ci], 5, 5, avoids=set()) is None
    assert st["carrying"] is None
    assert len(world.world_drops) == 1    # left where it lay


def test_act_stock_declines_when_the_creature_has_no_burrow(world):
    ci = 0
    isolate_creature(world, ci, 0)
    st = world.all_creature_stats[ci][0]
    st["home"] = None
    world._spawn_drop("berry", 1, 5, 5)

    assert world._act_stock(ci, 0, world.creature_defs[ci], 5, 5, avoids=set()) is None
    assert st["carrying"] is None


def test_act_stock_declines_when_its_burrow_has_collapsed(world):
    ci, st, burrow = _housed_rat(world, 5, 5)
    world.world_structures.clear()        # burrow gone, stale id left behind
    world._spawn_drop("berry", 1, 5, 5)

    assert world._act_stock(ci, 0, world.creature_defs[ci], 5, 5, avoids=set()) is None


def test_a_herbivore_never_stocks_because_its_diet_holds_no_items(world):
    # The rabbit eats grass and bushes, neither of which is a carryable item,
    # so its stock turn always declines and it moves on to the next need.
    ci = 1
    rabbit = world.creature_defs[ci]
    world._spawn_drop("berry", 1, 2, 2)

    assert world._resolve_diet(rabbit) == set()
    assert world._pick_up_drop_at(2, 2, rabbit) is None
    assert world._act_stock(ci, 0, rabbit, 2, 2, avoids=set()) is None


def test_act_on_need_ignores_a_task_it_has_no_behavior_for(world):
    assert world._act_on_need("thirst", 0, 0, world.creature_defs[0],
                              1, 1, avoids=set()) is None


def test_a_blocked_hauler_holds_its_ground_and_keeps_the_item(world):
    ci, st, _ = _housed_rat(world, 1, 1, home_x=4, home_z=1)
    st["carrying"] = "berry"
    for bx, bz in [(0, 1), (2, 1), (1, 0), (1, 2)]:
        world.chunk.set_block(bx, world.SY, bz, TREE)

    result = world._creature_move(ci, 0, world.creature_defs[ci], 1, 1,
                                  avoids={TREE})

    assert result == (1, 1)
    assert st["carrying"] == "berry"      # still holding it, tries again later


def test_a_declined_stock_turn_hands_over_to_the_next_need(world):
    # Nothing to hoard, so the creature should dig instead of standing idle.
    ci = 0
    isolate_creature(world, ci, 0)
    st = world.all_creature_stats[ci][0]
    st["hunger"] = world.creature_defs[ci]["initial_hunger"]
    st["sleep"] = 0.0
    st["home"] = None
    st["home_need"] = 0.5                 # ranked below stock's 0.9

    result = world._creature_move(ci, 0, world.creature_defs[ci], 4, 4, avoids=set())

    assert result == (4, 4)
    assert len(world.world_structures) == 1
    assert st["home"] == world.world_structures[0]["id"]


# ── stocking: hauling it home ────────────────────────────────────────────────

def test_a_carrying_creature_heads_home_instead_of_weighing_needs(world):
    ci, st, burrow = _housed_rat(world, 1, 1, home_x=4, home_z=1)
    st["carrying"] = "berry"
    st["hunger"] = 0                      # starving, and it still won't stop
    st["sleep"] = 5.0

    result = world._creature_move(ci, 0, world.creature_defs[ci], 1, 1, avoids=set())

    assert result == (2, 1)               # a step toward the burrow at (4, 1)
    assert st["asleep"] is False
    assert st["carrying"] == "berry"


def test_arriving_at_the_burrow_stashes_the_carried_item(world):
    ci, st, burrow = _housed_rat(world, 3, 1, home_x=4, home_z=1)
    st["carrying"] = "berry"

    result = world._creature_move(ci, 0, world.creature_defs[ci], 3, 1, avoids=set())

    assert result == (4, 1)
    assert st["carrying"] is None
    assert burrow["contains"] == [{"item": "berry", "count": 1}]


def test_standing_on_the_burrow_while_loaded_stashes_immediately(world):
    ci, st, burrow = _housed_rat(world, 5, 5)
    st["carrying"] = "seed"

    result = world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    assert result == (5, 5)
    assert st["carrying"] is None
    assert burrow["contains"] == [{"item": "seed", "count": 1}]


def test_stashing_the_same_item_twice_stacks_it(world):
    ci, st, burrow = _housed_rat(world, 5, 5)
    st["carrying"] = "berry"
    world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())
    st["carrying"] = "berry"
    world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    assert burrow["contains"] == [{"item": "berry", "count": 2}]


def test_stashing_a_different_item_adds_a_second_entry(world):
    ci, st, burrow = _housed_rat(world, 5, 5)
    st["carrying"] = "berry"
    world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())
    st["carrying"] = "seed"
    world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    assert burrow["contains"] == [{"item": "berry", "count": 1},
                                  {"item": "seed", "count": 1}]


def test_stashing_bumps_the_structure_revision(world):
    ci, st, _ = _housed_rat(world, 5, 5)
    st["carrying"] = "berry"
    before = world.structure_revision

    world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    assert world.structure_revision > before


def test_losing_the_burrow_mid_haul_puts_the_item_back_on_the_ground(world):
    ci, st, _ = _housed_rat(world, 1, 1, home_x=4, home_z=1)
    st["carrying"] = "berry"
    world.world_structures.clear()

    result = world._creature_move(ci, 0, world.creature_defs[ci], 1, 1, avoids=set())

    assert result == (1, 1)
    assert st["carrying"] is None
    assert [(d["item"], d["count"], d["x"], d["z"]) for d in world.world_drops] == [
        ("berry", 1, 1, 1)]


def test_a_creature_that_dies_carrying_something_drops_it(world):
    ci, st, _ = _housed_rat(world, 5, 5)
    st["carrying"] = "berry"

    world._remove_creature(ci, 0)

    assert ("berry", 1) in [(d["item"], d["count"]) for d in world.world_drops]


def test_the_carried_item_survives_a_reload(world):
    ci, st, _ = _housed_rat(world, 1, 1, home_x=4, home_z=1)
    st["carrying"] = "berry"

    world.save()
    reloaded = server_module.World()

    assert any(s.get("carrying") == "berry"
               for s in reloaded.all_creature_stats[ci])


def test_a_stashed_larder_survives_a_reload(world):
    ci, st, burrow = _housed_rat(world, 5, 5)
    st["carrying"] = "berry"
    world._creature_move(ci, 0, world.creature_defs[ci], 5, 5, avoids=set())

    world.save()
    reloaded = server_module.World()

    assert reloaded.world_structures[0]["contains"] == [
        {"item": "berry", "count": 1}]


def test_snapshot_reports_what_a_creature_is_carrying(world):
    ci, st, _ = _housed_rat(world, 5, 5)
    st["carrying"] = "berry"

    snap = world.snapshot()

    c = next(c for c in snap["creatures"] if c["id"] == st["id"])
    assert c["carrying"] == "berry"
    assert all(other["carrying"] is None
               for other in snap["creatures"] if other["id"] != st["id"])


# ── structures: weathering at season start ───────────────────────────────────

def test_break_structures_damages_a_burrow_when_the_roll_succeeds(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)  # always breaks

    world._break_structures()

    assert world.world_structures[0]["age"] == 1


def test_break_structures_leaves_a_burrow_alone_when_the_roll_fails(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.99)

    world._break_structures()

    assert world.world_structures[0]["age"] == 2


def test_break_structures_uses_the_declared_break_chance(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    # break_chance is 0.2, so a roll of exactly 0.2 must not break it.
    monkeypatch.setattr(server_module.random, "random", lambda: 0.2)

    world._break_structures()

    assert world.world_structures[0]["age"] == 2


def test_a_burrow_is_removed_once_its_age_reaches_zero(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)

    world._break_structures()   # age 2 -> 1
    world._break_structures()   # age 1 -> 0, collapses

    assert world.world_structures == []


def test_a_collapsing_burrow_evicts_its_dwellers(world, monkeypatch):
    ci = 0
    st = world.all_creature_stats[ci][0]
    world._act_home(ci, 0, world.creature_defs[ci], 1, 1, avoids=set())
    assert st["home"] is not None
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)

    world._break_structures()
    world._break_structures()

    assert st["home"] is None
    # Homeless again, so the want starts building back up.
    world._on_day_start()
    assert st["home_need"] == 0.5


def test_damaging_a_burrow_does_not_evict_its_dwellers(world, monkeypatch):
    ci = 0
    st = world.all_creature_stats[ci][0]
    world._act_home(ci, 0, world.creature_defs[ci], 1, 1, avoids=set())
    burrow_id = st["home"]
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)

    world._break_structures()

    assert st["home"] == burrow_id


def test_season_start_weathers_structures(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)

    world._on_season_start("summer")

    assert world.world_structures[0]["age"] == 1


def test_break_structures_bumps_the_structure_revision(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)
    before = world.structure_revision

    world._break_structures()

    assert world.structure_revision > before


def test_structure_revision_is_untouched_when_nothing_breaks(world, monkeypatch):
    world._build_structure(world.structure_defs[0], 1, 1)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.99)
    before = world.structure_revision

    world._break_structures()

    assert world.structure_revision == before


# ── structures: persistence ─────────────────────────────────────────────────

def test_structures_are_restored_from_the_world_file(world):
    ci = 0
    world._act_home(ci, 0, world.creature_defs[ci], 3, 4, avoids=set())
    burrow_id = world.world_structures[0]["id"]

    world.save()
    reloaded = server_module.World()

    assert len(reloaded.world_structures) == 1
    burrow = reloaded.world_structures[0]
    assert (burrow["id"], burrow["type"], burrow["x"], burrow["z"]) == (
        burrow_id, "burrow", 3, 4)
    assert burrow["age"] == 2


def test_a_creatures_home_survives_a_reload(world):
    ci = 0
    world._act_home(ci, 0, world.creature_defs[ci], 3, 4, avoids=set())
    burrow_id = world.all_creature_stats[ci][0]["home"]

    world.save()
    reloaded = server_module.World()

    assert reloaded.all_creature_stats[ci][0]["home"] == burrow_id


def test_save_writes_structures_into_the_world_file(world, isolated_paths):
    world._act_home(0, 0, world.creature_defs[0], 2, 5, avoids=set())

    world.save()

    raw = json.loads(isolated_paths["world_path"].read_text(encoding="utf-8"))
    assert raw["structures"] == [{
        "id": world.world_structures[0]["id"], "type": "burrow",
        "x": 2, "z": 5, "age": 2, "contains": [],
    }]
    assert raw["next_structure_id"] >= world.world_structures[0]["id"]


def test_new_burrows_do_not_reuse_a_restored_id(world):
    world._act_home(0, 0, world.creature_defs[0], 1, 1, avoids=set())
    world.save()

    reloaded = server_module.World()
    reloaded._act_home(0, 1, reloaded.creature_defs[0], 4, 4, avoids=set())

    ids = [s["id"] for s in reloaded.world_structures]
    assert len(set(ids)) == 2


def test_a_home_pointing_at_a_vanished_structure_is_cleared_on_load(world, isolated_paths):
    world._act_home(0, 0, world.creature_defs[0], 1, 1, avoids=set())
    world.save()
    raw = json.loads(isolated_paths["world_path"].read_text(encoding="utf-8"))
    raw["structures"] = []   # burrow gone, creature still references it
    isolated_paths["world_path"].write_text(json.dumps(raw), encoding="utf-8")

    reloaded = server_module.World()

    assert reloaded.all_creature_stats[0][0]["home"] is None


def test_a_saved_structure_of_an_unknown_type_is_dropped_on_load(world, isolated_paths):
    world._act_home(0, 0, world.creature_defs[0], 1, 1, avoids=set())
    world.save()
    raw = json.loads(isolated_paths["world_path"].read_text(encoding="utf-8"))
    raw["structures"][0]["type"] = "treehouse"   # no such definition
    isolated_paths["world_path"].write_text(json.dumps(raw), encoding="utf-8")

    reloaded = server_module.World()

    assert reloaded.world_structures == []
    assert reloaded.all_creature_stats[0][0]["home"] is None


# ── structures: snapshot ────────────────────────────────────────────────────

def test_snapshot_lists_structures_with_expected_fields(world):
    world._act_home(0, 0, world.creature_defs[0], 2, 2, avoids=set())

    snap = world.snapshot()

    assert len(snap["structures"]) == 1
    s = snap["structures"][0]
    assert set(s.keys()) == {"id", "type", "x", "z", "age", "contains"}
    assert (s["type"], s["x"], s["z"], s["age"]) == ("burrow", 2, 2, 2)


def test_snapshot_reports_the_structure_revision_and_creature_home(world):
    snap = world.snapshot()
    assert snap["structure_revision"] == 0
    assert all(c["home"] is None for c in snap["creatures"])

    world._act_home(0, 0, world.creature_defs[0], 2, 2, avoids=set())
    snap = world.snapshot()

    assert snap["structure_revision"] == 1
    assert any(c["home"] == snap["structures"][0]["id"] for c in snap["creatures"])


# ── clock persistence ────────────────────────────────────────────────────────

def test_a_fresh_world_file_starts_the_clock_at_spring_day_zero(world):
    assert world.current_season == "spring"
    assert world.current_cycle == 0
    assert world.current_day == 0


def test_clock_is_restored_from_the_world_file(world):
    world.current_cycle = 41
    world.current_day = 6
    world._apply_season("fall")

    world.save()
    reloaded = server_module.World()

    assert reloaded.current_cycle == 41
    assert reloaded.current_day == 6
    assert reloaded.current_season == "fall"


def test_save_writes_the_clock_into_the_world_file(world, isolated_paths):
    world.current_cycle = 12
    world.current_day = 3
    world._apply_season("winter")

    world.save()

    raw = json.loads(isolated_paths["world_path"].read_text(encoding="utf-8"))
    assert raw["time"] == {"cycle": 12, "season": "winter", "day": 3}


def test_restored_season_reapplies_its_moisture_and_fertility(world):
    world._apply_season("winter")
    world.save()

    reloaded = server_module.World()

    winter = server_module.load_config()["seasons"]["winter"]
    assert reloaded.chunk.moisture == winter["moisture"]
    assert reloaded.chunk.fertility == winter["fertility"]
    assert reloaded._terrain_texture == f'textures/{winter["texture"]}'


def test_a_saved_season_missing_from_config_falls_back_to_the_default(world, capsys):
    # config.json can be edited between runs; an unknown season name must not
    # take down startup with a KeyError.
    world.chunk.season = "monsoon"
    world.chunk.save(server_module.WORLD_FILE)

    reloaded = server_module.World()

    assert reloaded.current_season == server_module.DEFAULT_SEASON
    assert "monsoon" in capsys.readouterr().out


def test_a_negative_saved_cycle_or_day_is_clamped_to_zero(world):
    world.chunk.cycle = -5
    world.chunk.day = -1
    world.chunk.season = "spring"
    world.chunk.save(server_module.WORLD_FILE)

    reloaded = server_module.World()

    assert reloaded.current_cycle == 0
    assert reloaded.current_day == 0


@pytest.mark.parametrize("now, expected_is_day", [
    (0.0, True),    # phase 0.0  -> morning
    (50.0, False),  # phase 0.83 -> night (day_night_cycle is 60 in TEST_CONFIG)
])
def test_startup_seeds_prev_is_day_from_the_current_phase(
    isolated_paths, monkeypatch, now, expected_is_day
):
    monkeypatch.setattr(server_module.time, "time", lambda: now)

    assert server_module.World()._prev_is_day is expected_is_day


def test_a_server_started_at_night_still_counts_the_following_dawn(
    isolated_paths, monkeypatch
):
    monkeypatch.setattr(server_module.time, "time", lambda: 50.0)  # night
    night_world = server_module.World()
    before_day = night_world.current_day

    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)  # dawn
    night_world.tick(dt=0.01)

    assert night_world.current_day == before_day + 1


# ── fauna persistence ────────────────────────────────────────────────────────

def test_fauna_is_restored_from_the_world_file_instead_of_reseeded(world):
    ci = 0
    world.all_creature_positions[ci] = [(2, 3)]
    world.all_creature_stats[ci] = [{
        "id": 42, "age": 1, "hunger": 2, "attack": 5,
        "sleep": 0.5, "asleep": True, "home": None, "home_need": 1.5,
        "carrying": None,
    }]

    world.save()
    reloaded = server_module.World()

    assert reloaded.all_creature_positions[ci] == [(2, 3)]
    assert reloaded.all_creature_stats[ci] == [{
        "id": 42, "age": 1, "hunger": 2, "attack": 5,
        "sleep": 0.5, "asleep": True, "home": None, "home_need": 1.5,
        "carrying": None,
    }]


def test_restored_fauna_keeps_ids_unique_for_newly_born_creatures(world):
    ci = 0
    world.all_creature_positions[ci] = [(1, 1)]
    world.all_creature_stats[ci] = [{
        "id": 99, "age": 2, "hunger": 3, "attack": 5,
        "sleep": 0.0, "asleep": False,
    }]

    world.save()
    reloaded = server_module.World()
    reloaded._spawn_creature_at(ci, 2, 2)

    assert reloaded.all_creature_stats[ci][-1]["id"] > 99


def test_a_world_whose_fauna_all_died_stays_empty_after_reload(world):
    # Distinct from a pre-fauna save file: an explicitly empty list must not
    # be treated as "no data, please reseed".
    for ci in range(len(world.creature_defs)):
        world.all_creature_positions[ci] = []
        world.all_creature_stats[ci] = []

    world.save()
    reloaded = server_module.World()

    assert reloaded.all_creature_positions == [[] for _ in world.creature_defs]


def test_creature_type_missing_from_the_world_file_is_seeded(world, isolated_paths):
    # Adding a species to entities.json must not require regenerating the
    # world: saved types are restored, unknown ones spawn fresh.
    world.save()
    raw = json.loads(isolated_paths["world_path"].read_text(encoding="utf-8"))
    del raw["creatures"]["rabbit"]
    isolated_paths["world_path"].write_text(json.dumps(raw), encoding="utf-8")

    reloaded = server_module.World()

    assert len(reloaded.all_creature_positions[0]) == len(world.all_creature_positions[0])
    assert len(reloaded.all_creature_positions[1]) == 2  # rabbit count from entities.json


def test_save_writes_live_fauna_into_the_world_file(world, isolated_paths):
    ci = 0
    world.all_creature_positions[ci] = [(4, 5)]
    world.all_creature_stats[ci] = [{
        "id": 7, "age": 2, "hunger": 1, "attack": 5,
        "sleep": 0.0, "asleep": False, "home": 3, "home_need": 0.0,
        "carrying": "berry",
    }]

    world.save()

    raw = json.loads(isolated_paths["world_path"].read_text(encoding="utf-8"))
    assert raw["creatures"]["rat"] == [{
        "id": 7, "x": 4, "z": 5, "age": 2, "hunger": 1,
        "attack": 5, "sleep": 0.0, "asleep": False,
        "home": 3, "home_need": 0.0, "carrying": "berry",
    }]
    assert raw["next_creature_id"] >= 7


def test_seed_creatures_falls_back_when_every_tile_is_avoided(world):
    # rat avoids tiles tagged "tree"; force every tile to be a tree and
    # confirm _seed_creatures still returns a position for every requested
    # creature (via its "give up searching" fallback branch) instead of
    # crashing or under-spawning.
    for x in range(world.sx):
        for z in range(world.sz):
            world.chunk.set_block(x, world.SY, z, TREE)
    world.all_creature_positions = []
    world.all_creature_stats = []

    world._seed_creatures()

    assert len(world.all_creature_positions[0]) == 2
    assert len(world.all_creature_positions[1]) == 2


# ── config hot reload ────────────────────────────────────────────────────────

def test_maybe_reload_config_picks_up_changed_file(world, isolated_paths):
    cfg = json.loads(isolated_paths["config_path"].read_text())
    cfg["season_length"] = 99
    isolated_paths["config_path"].write_text(json.dumps(cfg), encoding="utf-8")
    # nudge mtime forward in case the filesystem's clock resolution is coarse
    st = isolated_paths["config_path"].stat()
    os.utime(isolated_paths["config_path"], (st.st_atime, st.st_mtime + 5))

    world.maybe_reload_config()

    assert world.season_length == 99


def test_maybe_reload_config_noop_when_unchanged(world):
    world.maybe_reload_config()  # establishes the baseline mtime
    original_season_length = world.season_length

    world.maybe_reload_config()  # same file, same mtime -> should short-circuit

    assert world.season_length == original_season_length


def test_maybe_reload_config_handles_missing_config_file_gracefully(world, monkeypatch, isolated_paths):
    missing_path = isolated_paths["config_path"].parent / "does_not_exist.json"
    monkeypatch.setattr(server_module, "CONFIG_PATH", missing_path)
    before_season_length = world.season_length

    world.maybe_reload_config()  # must not raise -- just return early

    assert world.season_length == before_season_length


# ── admin: speed multiplier ──────────────────────────────────────────────────

def test_speed_multiplier_defaults_to_one(world):
    assert world.speed_multiplier == 1
    assert world.admin_state() == {"speed_multiplier": 1}


def test_set_speed_multiplier_accepts_the_full_valid_range(world):
    assert world.set_speed_multiplier(1) == 1
    assert world.speed_multiplier == 1
    assert world.set_speed_multiplier(100) == 100
    assert world.speed_multiplier == 100
    assert world.set_speed_multiplier(50) == 50
    assert world.admin_state() == {"speed_multiplier": 50}


def test_set_speed_multiplier_accepts_numeric_strings(world):
    # HTTP JSON bodies could plausibly send "10" -- int() coerces this fine.
    assert world.set_speed_multiplier("10") == 10


@pytest.mark.parametrize("bad_value", [0, -1, 101, 1000])
def test_set_speed_multiplier_rejects_out_of_range_values(world, bad_value):
    with pytest.raises(ValueError):
        world.set_speed_multiplier(bad_value)
    assert world.speed_multiplier == 1  # unchanged after a rejected update


@pytest.mark.parametrize("bad_value", ["fast", None, [1], {}])
def test_set_speed_multiplier_rejects_non_numeric_values(world, bad_value):
    with pytest.raises(ValueError):
        world.set_speed_multiplier(bad_value)
    assert world.speed_multiplier == 1


def test_effective_time_equals_wall_clock_at_default_multiplier(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 12345.0)
    assert world._effective_time() == 12345.0


def test_tick_at_default_multiplier_advances_phase_by_real_elapsed_time(world, monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr(server_module.time, "time", lambda: fake_now[0])

    world.tick(dt=1.0)
    phase_at_t0 = world._phase

    fake_now[0] = 5.0
    world.tick(dt=1.0)
    phase_at_t5 = world._phase

    # with speed_multiplier == 1, phase tracks the real wall clock 1:1
    assert phase_at_t5 - phase_at_t0 == pytest.approx(5.0 / world.day_night_cycle)


def test_tick_at_higher_multiplier_advances_phase_faster_than_wall_clock(world, monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr(server_module.time, "time", lambda: fake_now[0])
    world.set_speed_multiplier(10)

    dt = 1.0
    fake_now[0] += dt  # 1 real second actually elapsing, consistent with dt
    world.tick(dt=dt)

    # 1 real second at 10x multiplier -> 10 virtual seconds of phase progress
    assert world._phase == pytest.approx(10.0 / world.day_night_cycle)


def test_tick_at_higher_multiplier_speeds_up_creature_movement_timer(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)  # is_day True
    ci = 0
    world.set_speed_multiplier(10)
    interval = world.creature_defs[ci]["move_interval_day"]

    # a real dt that alone wouldn't reach the interval, but does at 10x speed
    world.tick(dt=(interval / 10.0) + 0.01)

    assert world._creature_timers[ci] == 0.0  # fired and reset this tick


def test_tick_at_higher_multiplier_speeds_up_the_flora_sim_cycle(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    world.set_speed_multiplier(10)

    world.tick(dt=(world.cycle_length / 10.0) + 0.01)

    assert world.current_cycle == 1


def test_tick_at_default_multiplier_is_unaffected_by_the_offset_mechanism(world, monkeypatch):
    # sanity check: the speed_multiplier plumbing must be a true no-op at 1x
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    world.tick(dt=1.0)
    assert world._time_offset == 0.0


# ── season application ───────────────────────────────────────────────────────

def test_apply_season_updates_chunk_and_texture(world):
    world._apply_season("winter")

    assert world.current_season == "winter"
    assert world.chunk.moisture == 30
    assert world.chunk.fertility == 10
    assert world._terrain_texture == "textures/soil_winter.png"


# ── tag / diet / avoidance resolution ────────────────────────────────────────

def test_resolve_diet_expands_tag_to_matching_items(world):
    cdef = world.creature_defs[0]
    assert world._resolve_diet(cdef) == {"seed", "berry", "meat",
                                         "carrot", "cabbage"}


def test_resolve_diet_accepts_direct_item_names(world):
    assert world._resolve_diet({"diet": ["seed"]}) == {"seed"}


def test_resolve_diet_empty_for_no_diet(world):
    assert world._resolve_diet({}) == set()


def test_resolve_avoids_uses_tag(world):
    cdef = world.creature_defs[0]
    assert world._resolve_avoids(cdef) == {TREE}


def test_resolve_avoids_falls_back_to_direct_block_id(world):
    assert world._resolve_avoids({"avoids_block": BUSH}) == {BUSH}


def test_resolve_avoids_empty_when_unspecified(world):
    assert world._resolve_avoids({}) == set()


def test_get_stage_picks_first_stage_whose_max_age_covers_the_given_age(world):
    vdef = world.veg_defs[FLOWER]
    dry_stage = world._get_stage(vdef, age=1)
    fresh_stage = world._get_stage(vdef, age=2)

    assert dry_stage["texture"].endswith("flower_dry.png")
    assert fresh_stage["texture"].endswith("flower.png")


def test_get_stage_clamps_to_last_stage_for_overflow_age(world):
    vdef = world.veg_defs[TREE]
    assert world._get_stage(vdef, age=9999) is vdef["stages"][-1]


def test_count_kind_near_counts_within_radius(world):
    world.chunk.set_block(2, world.SY, 2, TREE)
    world.chunk.set_block(3, world.SY, 2, TREE)
    world.chunk.set_block(5, world.SY, 5, TREE)  # far away, outside radius 1

    assert world._count_kind_near(2, 2, TREE, radius=1) == 2
    assert world._count_kind_near(2, 2, TREE, radius=0) == 1


# ── drops: spawn / expire / pickup ───────────────────────────────────────────

def test_spawn_drop_appends_with_unique_incrementing_id(world):
    world._spawn_drop("seed", 3, 1, 1)
    world._spawn_drop("berry", 1, 2, 2)

    assert [d["item"] for d in world.world_drops] == ["seed", "berry"]
    ids = [d["id"] for d in world.world_drops]
    assert ids[1] == ids[0] + 1


def test_drop_from_uses_stage_specific_contains(world):
    flower = world.veg_defs[FLOWER]

    world._drop_from(flower, 1, 1, age=1)  # dry stage: guaranteed exactly 1 seed

    assert len(world.world_drops) == 1
    assert world.world_drops[0]["item"] == "seed"
    assert world.world_drops[0]["count"] == 1


def test_drop_from_skips_zero_count_rolls(world, monkeypatch):
    flower = world.veg_defs[FLOWER]
    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)

    world._drop_from(flower, 1, 1, age=99)  # fresh stage: count range is [0, 1]

    assert world.world_drops == []


def test_update_drops_expires_after_lifetime(world):
    world._spawn_drop("seed", 1, 0, 0)
    world.world_drops[0]["spawn_time"] = time_module.time() - (world.drop_lifetime + 1)

    world._update_drops()

    assert world.world_drops == []


def test_update_drops_picked_up_by_adjacent_hungry_creature(world):
    ci = 0
    isolate_creature(world, ci, 0)
    world.all_creature_positions[ci][0] = (0, 0)
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["asleep"] = False
    world._spawn_drop("seed", 2, 1, 0)  # adjacent tile (manhattan distance 1)

    world._update_drops()

    assert world.world_drops == []
    assert world.all_creature_stats[ci][0]["hunger"] == 2  # gained 2 x hunger_per_food(1)


def test_update_drops_left_alone_by_an_adjacent_full_creature(world):
    # A creature that can't eat any of it must not consume the stack -- and a
    # stocking creature is full by definition, so this is the normal case for
    # anything hauling food home.
    ci = 0
    isolate_creature(world, ci, 0)
    world.all_creature_positions[ci][0] = (0, 0)
    world.all_creature_stats[ci][0]["hunger"] = world.creature_defs[ci]["initial_hunger"]
    world.all_creature_stats[ci][0]["asleep"] = False
    world._spawn_drop("seed", 2, 1, 0)

    world._update_drops()

    assert len(world.world_drops) == 1
    assert world.world_drops[0]["count"] == 2


def test_update_drops_leaves_the_remainder_of_a_partly_eaten_stack(world):
    ci = 0
    isolate_creature(world, ci, 0)
    world.all_creature_positions[ci][0] = (0, 0)
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = cdef["initial_hunger"] - 1
    world.all_creature_stats[ci][0]["asleep"] = False
    world._spawn_drop("seed", 3, 1, 0)    # only room for one

    world._update_drops()

    assert world.all_creature_stats[ci][0]["hunger"] == cdef["initial_hunger"]
    assert world.world_drops[0]["count"] == 2


def test_update_drops_not_picked_up_by_sleeping_creature(world):
    ci = 0
    isolate_creature(world, ci, 0)
    world.all_creature_positions[ci][0] = (0, 0)
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["asleep"] = True
    world._spawn_drop("seed", 1, 0, 0)

    world._update_drops()

    assert len(world.world_drops) == 1  # left untouched
    assert world.all_creature_stats[ci][0]["hunger"] == 0


def test_update_drops_ignored_for_non_matching_diet(world):
    isolate_creature(world, 0, 0)
    world.all_creature_positions[0][0] = (0, 0)
    world.all_creature_stats[0][0]["hunger"] = 0
    world._spawn_drop("log", 1, 0, 0)  # "log" is not in the rat's "food" diet

    world._update_drops()

    assert len(world.world_drops) == 1


# ── flower attack / feeding at a block ───────────────────────────────────────

def test_is_flower_at_and_dead(world):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 1
    assert world._is_flower_at(1, 1) is True
    assert world._is_flower_dead(1, 1) is True

    world.chunk.vegetation_ages[(1, 1)] = 2
    assert world._is_flower_dead(1, 1) is False


def test_is_flower_dead_false_when_no_flower_present(world):
    world.chunk.set_block(4, world.SY, 4, GRASS)
    assert world._is_flower_dead(4, 4) is False


def test_attack_flower_reduces_age_without_killing(world):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 2
    before_rev = world.vegetation_revision

    hit = world._attack_flower_at(1, 1, attack_value=1)

    assert hit is True
    assert world.chunk.get_block(1, world.SY, 1) == FLOWER
    assert world.chunk.vegetation_ages[(1, 1)] == 1
    assert world.vegetation_revision == before_rev + 1


def test_attack_flower_kills_and_drops_loot_when_age_hits_zero(world):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 1

    world._attack_flower_at(1, 1, attack_value=5)

    assert world.chunk.get_block(1, world.SY, 1) == GRASS
    assert (1, 1) not in world.chunk.vegetation_ages
    assert any(d["item"] == "seed" for d in world.world_drops)


def test_attack_flower_returns_false_when_no_flower_present(world):
    world.chunk.set_block(2, world.SY, 2, GRASS)
    assert world._attack_flower_at(2, 2, attack_value=1) is False


def test_eat_food_at_block_consumes_matching_drop_up_to_hunger_cap(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 1
    world._spawn_drop("berry", 3, 4, 4)

    ate = world._eat_food_at_block(4, 4, ci, 0, world.creature_defs[ci])

    assert ate is True
    assert world.all_creature_stats[ci][0]["hunger"] == 3  # capped at initial_hunger
    assert world.world_drops[0]["count"] == 1  # 2 consumed, 1 left in the drop


def test_eat_food_at_block_false_when_already_full(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 3  # already at max
    world._spawn_drop("berry", 1, 4, 4)

    ate = world._eat_food_at_block(4, 4, ci, 0, world.creature_defs[ci])

    assert ate is False
    assert world.world_drops[0]["count"] == 1  # untouched


def test_eat_food_at_block_false_when_no_edible_drop_on_tile(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 0
    world._spawn_drop("berry", 1, 9, 9)  # a different tile

    ate = world._eat_food_at_block(4, 4, ci, 0, world.creature_defs[ci])

    assert ate is False


def test_eat_food_at_block_false_when_creature_has_no_diet(world):
    ate = world._eat_food_at_block(0, 0, ci=0, i=0, cdef={})
    assert ate is False


# ── nearest-target search + movement primitives ──────────────────────────────

def test_find_nearest_food_drop_returns_closest_within_radius(world):
    world._spawn_drop("seed", 1, 0, 0)
    world._spawn_drop("seed", 1, 5, 5)

    target = world._find_nearest_food_drop(1, 0, world.creature_defs[0], radius=5)

    assert target == (0, 0)


def test_find_nearest_food_drop_none_when_out_of_radius(world):
    world._spawn_drop("seed", 1, 5, 5)

    target = world._find_nearest_food_drop(0, 0, world.creature_defs[0], radius=1)

    assert target is None


def test_find_nearest_food_drop_none_when_creature_has_no_diet(world):
    world._spawn_drop("seed", 1, 0, 0)
    assert world._find_nearest_food_drop(0, 0, cdef={}, radius=5) is None


def test_find_nearest_food_drop_skips_items_outside_the_diet(world):
    world._spawn_drop("log", 1, 1, 1)   # not in the rat's "food" diet
    world._spawn_drop("seed", 1, 3, 3)  # matches

    target = world._find_nearest_food_drop(0, 0, world.creature_defs[0], radius=10)

    assert target == (3, 3)


def test_find_nearest_flower_distinguishes_dead_from_alive(world):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 1  # dead/dry
    world.chunk.set_block(2, world.SY, 2, FLOWER)
    world.chunk.vegetation_ages[(2, 2)] = 2  # alive

    dead_target = world._find_nearest_flower(0, 0, dead=True, radius=5)
    live_target = world._find_nearest_flower(0, 0, dead=False, radius=5)

    assert dead_target == (1, 1)
    assert live_target == (2, 2)


def test_find_nearest_flower_none_when_no_flower_vegetation_defined(world, monkeypatch):
    monkeypatch.setattr(world, "flower_vdef", None)
    assert world._find_nearest_flower(0, 0, dead=False) is None


def test_find_nearest_flower_ignores_targets_beyond_the_manhattan_radius(world):
    # (2, 2) sits inside the search's bounding box for radius=2 (fx, fz in
    # [0, 2]) but its Manhattan distance from (0, 0) is 4, which must still
    # be excluded by the tighter per-candidate radius check.
    world.chunk.set_block(2, world.SY, 2, FLOWER)
    world.chunk.vegetation_ages[(2, 2)] = 2

    assert world._find_nearest_flower(0, 0, dead=False, radius=2) is None


def test_step_toward_prefers_the_unique_direct_path_when_unblocked(world):
    step = world._step_toward(0, 0, 5, 0, avoids=set())
    assert step == (1, 0)


def test_step_toward_avoids_blocked_tile_and_returns_valid_neighbor(world):
    world.chunk.set_block(1, world.SY, 0, TREE)  # blocks the direct step east

    step = world._step_toward(0, 0, 5, 0, avoids={TREE})

    assert step is not None
    nx, nz = step
    assert (nx, nz) != (1, 0)
    assert 0 <= nx < world.sx and 0 <= nz < world.sz


def test_step_toward_returns_none_when_fully_boxed_in(world):
    for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        world.chunk.set_block(1 + dx, world.SY, 1 + dz, TREE)

    step = world._step_toward(1, 1, 5, 5, avoids={TREE})

    assert step is None


def test_move_creature_random_stays_put_when_every_neighbor_is_blocked(world):
    for dx, dz in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
        world.chunk.set_block(2 + dx, world.SY, 2 + dz, TREE)

    result = world._move_creature_random(2, 2, avoids={TREE})

    assert result == (2, 2)


def test_move_creature_random_returns_an_adjacent_tile_when_unblocked(world):
    nx, nz = world._move_creature_random(2, 2, avoids=set())
    assert abs(nx - 2) + abs(nz - 2) == 1


# ── feed_radius ───────────────────────────────────────────────────────────────

def test_feed_radius_comes_from_the_creature_definition(world):
    assert world._feed_radius({"feed_radius": 9}) == 9


def test_feed_radius_falls_back_to_the_default_when_undeclared(world):
    assert world._feed_radius({}) == server_module.DEFAULT_FEED_RADIUS


@pytest.mark.parametrize("value", ["far", None, [3]])
def test_feed_radius_falls_back_to_the_default_for_an_unusable_value(world, value):
    assert world._feed_radius({"feed_radius": value}) == server_module.DEFAULT_FEED_RADIUS


def test_feed_radius_clamps_a_negative_value_to_zero(world):
    assert world._feed_radius({"feed_radius": -4}) == 0


def test_rat_and_rabbit_both_declare_a_feed_radius(world):
    # Both definitions are explicit so behavior doesn't hinge on the fallback.
    for cdef in world.creature_defs:
        assert "feed_radius" in cdef


def test_act_feed_chases_a_food_drop_inside_the_rats_feed_radius(world):
    ci = 0
    cdef = world.creature_defs[ci]
    cdef["feed_radius"] = 5
    world.all_creature_stats[ci][0]["hunger"] = 0
    world._spawn_drop("seed", 1, 4, 0)  # 4 tiles away, within the radius

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == (1, 0)  # first step toward the drop


def test_act_feed_ignores_a_food_drop_outside_the_rats_feed_radius(world, monkeypatch):
    ci = 0
    cdef = world.creature_defs[ci]
    cdef["feed_radius"] = 2
    world.all_creature_stats[ci][0]["hunger"] = 0
    world._spawn_drop("seed", 1, 4, 0)  # 4 tiles away, out of reach now
    wandered = ("wandered",)
    monkeypatch.setattr(world, "_move_creature_random", lambda *a: wandered)

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == wandered


def test_act_feed_ignores_a_flower_outside_the_rats_feed_radius(world, monkeypatch):
    ci = 0
    cdef = world.creature_defs[ci]
    cdef["feed_radius"] = 2
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.chunk.set_block(2, world.SY, 2, FLOWER)  # 4 tiles away
    world.chunk.vegetation_ages[(2, 2)] = 1
    wandered = ("wandered",)
    monkeypatch.setattr(world, "_move_creature_random", lambda *a: wandered)

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == wandered


def test_act_feed_searches_drops_and_flowers_with_the_declared_radius(world, monkeypatch):
    ci = 0
    cdef = world.creature_defs[ci]
    cdef["feed_radius"] = 4
    world.all_creature_stats[ci][0]["hunger"] = 0
    seen = []
    monkeypatch.setattr(world, "_find_nearest_food_drop",
                        lambda *a, radius: seen.append(radius))
    monkeypatch.setattr(world, "_find_nearest_flower",
                        lambda *a, dead, radius: seen.append(radius))

    world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert seen == [4, 4, 4]  # drops, then dead flowers, then live flowers


# ── _act_feed priority chain (eat > attack > chase food > chase flowers) ────

def test_act_feed_steps_toward_nearest_dead_flower_when_nothing_closer(world):
    ci = 0
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0  # hungry, nothing to eat in place
    world.chunk.set_block(2, world.SY, 2, FLOWER)
    world.chunk.vegetation_ages[(2, 2)] = 1  # dead/dry

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result != (0, 0)
    assert abs(result[0] - 0) + abs(result[1] - 0) == 1


def test_act_feed_steps_toward_nearest_live_flower_as_last_resort(world):
    ci = 0
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.chunk.set_block(2, world.SY, 2, FLOWER)
    world.chunk.vegetation_ages[(2, 2)] = 2  # alive, not dead

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result != (0, 0)
    assert abs(result[0] - 0) + abs(result[1] - 0) == 1


def test_act_feed_random_walk_when_nothing_edible_is_reachable(world):
    ci = 0
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    # no drops, no flowers anywhere on the board

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert abs(result[0] - 0) + abs(result[1] - 0) <= 1


def test_act_feed_attacks_flower_standing_on_the_same_tile(world):
    ci = 0
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["attack"] = 5
    world.chunk.set_block(0, world.SY, 0, FLOWER)
    world.chunk.vegetation_ages[(0, 0)] = 1  # dies in one hit at attack=5

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == (0, 0)
    assert world.chunk.get_block(0, world.SY, 0) == GRASS  # killed by the attack


def test_act_feed_steps_toward_nearby_food_drop_before_attacking_any_flower(world):
    ci = 0
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    world._spawn_drop("seed", 1, 2, 0)  # nearby, but not on the creature's own tile

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result != (0, 0)
    assert abs(result[0] - 0) + abs(result[1] - 0) == 1


# ── rabbit plant-diet feed AI ────────────────────────────────────────────────

def test_resolve_plant_diet_tiers_keeps_declared_order(world):
    tiers = world._resolve_plant_diet_tiers(world.creature_defs[1])
    assert [[p["name"] for p in tier] for tier in tiers] == [
        ["carrot", "cabbage"], ["grass"], ["bush"]]


def test_a_tag_diet_entry_becomes_one_tier_of_several_plants(world):
    # "crops" names two plants, so they share a preference tier rather than
    # carrot outranking cabbage by declaration order.
    tiers = world._resolve_plant_diet_tiers({"diet": ["crops"]})
    assert len(tiers) == 1
    assert {p["name"] for p in tiers[0]} == {"carrot", "cabbage"}


def test_resolve_plant_diet_tiers_drops_entries_matching_no_plant(world):
    assert world._resolve_plant_diet_tiers({"diet": ["food", "grass"]}) == [
        [world.veg_defs[GRASS_PATCH]]]


def test_resolve_plant_diet_tiers_empty_for_item_only_diet(world):
    assert world._resolve_plant_diet_tiers(world.creature_defs[0]) == []


def test_act_feed_rabbit_eats_grass_on_same_tile(world):
    ci = 1
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.chunk.set_block(0, world.SY, 0, GRASS_PATCH)
    world.chunk.vegetation_ages[(0, 0)] = 5
    before_rev = world.vegetation_revision

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == (0, 0)
    assert world.chunk.get_block(0, world.SY, 0) == GRASS
    assert (0, 0) not in world.chunk.vegetation_ages
    assert world.all_creature_stats[ci][0]["hunger"] == 1
    assert world.vegetation_revision == before_rev + 1


def test_act_feed_rabbit_seeks_grass_within_feed_radius_before_bush(world):
    ci = 1
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    # Bush is closer (1 step) than grass (3 steps) — grass still wins by diet order.
    world.chunk.set_block(1, world.SY, 0, BUSH)
    world.chunk.vegetation_ages[(1, 0)] = 5
    world.chunk.set_block(3, world.SY, 0, GRASS_PATCH)
    world.chunk.vegetation_ages[(3, 0)] = 5

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == (1, 0)  # first step toward grass at (3,0)


def test_act_feed_rabbit_browses_bush_when_no_grass_in_range(world):
    ci = 1
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["attack"] = 1
    world.chunk.set_block(0, world.SY, 0, BUSH)
    world.chunk.vegetation_ages[(0, 0)] = 5

    result = world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert result == (0, 0)
    assert world.chunk.get_block(0, world.SY, 0) == BUSH
    assert world.chunk.vegetation_ages[(0, 0)] == 4  # aged +1 toward death
    assert world.all_creature_stats[ci][0]["hunger"] == 1


def test_act_feed_rabbit_kills_bush_when_age_hits_zero(world):
    ci = 1
    cdef = world.creature_defs[ci]
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["attack"] = 1
    world.chunk.set_block(0, world.SY, 0, BUSH)
    world.chunk.vegetation_ages[(0, 0)] = 1

    world._act_feed(ci, 0, cdef, 0, 0, avoids=set())

    assert world.chunk.get_block(0, world.SY, 0) == GRASS
    assert (0, 0) not in world.chunk.vegetation_ages
    assert any(d["item"] == "berry" for d in world.world_drops)


# ── crops: rabbits browse them first, rats raid them last ───────────────────

def _hungry(world, ci, x, z):
    """One hungry, isolated creature of type ci standing at (x, z)."""
    isolate_creature(world, ci, 0)
    world.all_creature_positions[ci][0] = (x, z)
    st = world.all_creature_stats[ci][0]
    st["hunger"] = 0
    st["asleep"] = False
    st["attack"] = 1
    return world.creature_defs[ci], st


def _plant(world, x, z, bid, age=2):
    world.chunk.set_block(x, world.SY, z, bid)
    world.chunk.vegetation_ages[(x, z)] = age


def test_rabbit_browses_a_crop_on_its_own_tile(world):
    cdef, st = _hungry(world, 1, 2, 2)
    _plant(world, 2, 2, CARROT)

    result = world._act_feed(1, 0, cdef, 2, 2, avoids=set())

    assert result == (2, 2)
    assert world.chunk.vegetation_ages[(2, 2)] == 1   # aged by attack
    assert st["hunger"] == 1


def test_a_rabbit_browsing_a_ripe_crop_harvests_it(world):
    cdef, st = _hungry(world, 1, 2, 2)
    _plant(world, 2, 2, CABBAGE, age=1)

    world._act_feed(1, 0, cdef, 2, 2, avoids=set())

    assert world.chunk.get_block(2, world.SY, 2) == GRASS
    assert [(d["item"], d["count"]) for d in world.world_drops] == [("cabbage", 1)]


def test_rabbit_crosses_the_map_for_a_crop_before_eating_nearby_grass(world):
    cdef, _ = _hungry(world, 1, 0, 0)
    world.chunk.set_block(1, world.SY, 0, GRASS_PATCH)   # one step away
    world.chunk.vegetation_ages[(1, 0)] = 5
    _plant(world, 4, 0, CARROT)                          # four steps away

    result = world._act_feed(1, 0, cdef, 0, 0, avoids=set())

    assert result == (1, 0)   # a step toward the carrot, not onto the grass


def test_rabbit_takes_the_nearest_crop_regardless_of_which_kind(world):
    # Both are in the same diet tier, so distance decides -- carrot being
    # declared first must not drag the rabbit past a closer cabbage.
    cdef, _ = _hungry(world, 1, 0, 0)
    _plant(world, 4, 0, CARROT)
    _plant(world, 2, 0, CABBAGE)

    assert world._act_feed(1, 0, cdef, 0, 0, avoids=set()) == (1, 0)

    world.chunk.set_block(2, world.SY, 0, GRASS)   # closer cabbage harvested
    _plant(world, 0, 3, CABBAGE)                   # now one is nearer the other way

    assert world._act_feed(1, 0, cdef, 0, 0, avoids=set()) == (0, 1)


def test_rabbit_falls_back_to_grass_when_no_crop_is_in_range(world):
    cdef, _ = _hungry(world, 1, 0, 0)
    world.chunk.set_block(1, world.SY, 0, GRASS_PATCH)
    world.chunk.vegetation_ages[(1, 0)] = 5
    _plant(world, 5, 5, CARROT)   # 10 tiles away, feed_radius is 6

    result = world._act_feed(1, 0, cdef, 0, 0, avoids=set())

    assert result == (1, 0)
    assert world.chunk.get_block(1, world.SY, 0) == GRASS_PATCH  # stepped, not eaten


def test_rat_attacks_a_crop_it_is_standing_on(world):
    cdef, st = _hungry(world, 0, 2, 2)
    _plant(world, 2, 2, CARROT)

    result = world._act_feed(0, 0, cdef, 2, 2, avoids=set())

    assert result == (2, 2)
    assert world.chunk.vegetation_ages[(2, 2)] == 1
    assert st["hunger"] == 0   # attacking feeds nobody; the drop does that


def test_a_rat_knocking_down_a_crop_leaves_the_vegetable_to_eat(world):
    cdef, _ = _hungry(world, 0, 2, 2)
    _plant(world, 2, 2, CABBAGE, age=1)

    world._act_feed(0, 0, cdef, 2, 2, avoids=set())

    assert world.chunk.get_block(2, world.SY, 2) == GRASS
    assert [(d["item"], d["count"]) for d in world.world_drops] == [("cabbage", 1)]
    # ...and the vegetable is on the rat's menu, since its diet is the food tag
    assert "cabbage" in world._resolve_diet(cdef)


def test_rat_heads_for_a_crop_when_no_flower_is_in_range(world):
    cdef, _ = _hungry(world, 0, 0, 0)
    _plant(world, 3, 0, CARROT)

    result = world._act_feed(0, 0, cdef, 0, 0, avoids=set())

    assert result == (1, 0)


def test_rat_prefers_a_further_flower_over_a_closer_crop(world):
    cdef, _ = _hungry(world, 0, 0, 0)
    _plant(world, 1, 0, CARROT)                       # one step away
    world.chunk.set_block(0, world.SY, 3, FLOWER)     # three steps away
    world.chunk.vegetation_ages[(0, 3)] = 2

    result = world._act_feed(0, 0, cdef, 0, 0, avoids=set())

    assert result == (0, 1)   # toward the flower


def test_rat_prefers_a_food_drop_over_a_crop(world):
    cdef, _ = _hungry(world, 0, 0, 0)
    _plant(world, 1, 0, CARROT)
    world._spawn_drop("seed", 1, 0, 2)

    assert world._act_feed(0, 0, cdef, 0, 0, avoids=set()) == (0, 1)


def test_rat_wanders_when_neither_flower_nor_crop_is_in_range(world):
    cdef, _ = _hungry(world, 0, 2, 2)
    _plant(world, 5, 5, CARROT)   # 6 tiles away, feed_radius is 5

    result = world._act_feed(0, 0, cdef, 2, 2, avoids=set())

    assert world._manhattan(2, 2, *result) == 1   # a single random step
    assert world.chunk.get_block(5, world.SY, 5) == CARROT   # left standing


def test_crop_vdefs_are_resolved_from_the_tag(world):
    assert [v["name"] for v in world.crop_vdefs] == ["carrot", "cabbage"]


def test_crop_at_reports_the_plant_standing_there(world):
    _plant(world, 1, 1, CABBAGE)

    assert world._crop_at(1, 1)["name"] == "cabbage"
    assert world._crop_at(2, 2) is None


def test_attacking_a_plant_does_not_feed_the_attacker(world):
    _plant(world, 1, 1, CARROT)
    before = world.vegetation_revision

    assert world._attack_plant_at(1, 1, world.crop_vdefs[0], attack_value=1) is True
    assert world.chunk.vegetation_ages[(1, 1)] == 1
    assert world.vegetation_revision == before + 1


def test_attacking_a_plant_that_is_not_there_is_a_no_op(world):
    before = world.vegetation_revision

    assert world._attack_plant_at(1, 1, world.crop_vdefs[0], attack_value=1) is False
    assert world.vegetation_revision == before


def test_find_nearest_veg_among_ignores_the_searchers_own_tile(world):
    _plant(world, 1, 1, CARROT)
    _plant(world, 3, 1, CABBAGE)

    assert world._find_nearest_veg_among(1, 1, world.crop_vdefs, radius=5) == (3, 1)


def test_find_nearest_veg_among_is_empty_without_plants_to_look_for(world):
    assert world._find_nearest_veg_among(1, 1, [], radius=5) is None
    assert world._find_nearest_veg_among(1, 1, [None], radius=5) is None


def test_remove_rabbit_drops_two_to_three_meat(world):
    ci = 1
    x, z = world.all_creature_positions[ci][0]
    world.world_drops.clear()

    world._remove_creature(ci, 0)

    meat = [d for d in world.world_drops if d["item"] == "meat" and d["x"] == x and d["z"] == z]
    assert len(meat) == 1
    assert 2 <= meat[0]["count"] <= 3


def test_on_season_start_rabbit_reproduces_two_offspring(world):
    ci = 1
    world.all_creature_positions[ci] = [(2, 2)]
    world.all_creature_stats[ci] = [{
        "id": 200, "age": 3, "hunger": 5, "attack": 1,
        "sleep": 0.0, "asleep": False,
    }]

    world._on_season_start("spring")

    # fixture reproduce_count [2, 2] -> exactly two offspring
    assert len(world.all_creature_positions[ci]) == 3


# ── creature needs / sleep state machine ─────────────────────────────────────

def test_compute_creature_needs_feed_and_sleep(world):
    cdef = world.creature_defs[0]
    st = {"hunger": 1, "sleep": 0.7, "asleep": False}

    needs = world._compute_creature_needs(cdef, st)

    assert needs["feed"] == 2  # initial_hunger(3) - hunger(1)
    assert needs["sleep"] == 0.7


def test_compute_creature_needs_sleep_reads_as_zero_while_already_asleep(world):
    cdef = world.creature_defs[0]
    st = {"hunger": 3, "sleep": 0.9, "asleep": True}

    needs = world._compute_creature_needs(cdef, st)

    assert needs["sleep"] == 0


def test_ranked_needs_orders_by_value_strongest_first(world):
    assert world._ranked_needs({"feed": 2, "sleep": 0.5}) == ["feed", "sleep"]
    assert world._ranked_needs({"feed": 0.2, "sleep": 0.9}) == ["sleep", "feed"]


def test_ranked_needs_drops_anything_not_worth_acting_on(world):
    assert world._ranked_needs({"feed": 0, "sleep": 0}) == []
    assert world._ranked_needs({}) == []
    assert world._ranked_needs({"feed": 0, "stock": 0.9}) == ["stock"]


def test_ranked_needs_breaks_ties_in_declared_order(world):
    assert world._ranked_needs({"sleep": 0.9, "stock": 0.9}) == ["sleep", "stock"]
    assert world._ranked_needs({"stock": 0.9, "sleep": 0.9}) == ["stock", "sleep"]


def test_wake_and_sleep_creature_toggle_state(world):
    ci = 0
    world._sleep_creature(ci, 0, world.creature_defs[ci])
    assert world.all_creature_stats[ci][0]["asleep"] is True

    world.all_creature_stats[ci][0]["sleep"] = 0.8
    world._wake_creature(ci, 0, world.creature_defs[ci])

    assert world.all_creature_stats[ci][0]["asleep"] is False
    assert world.all_creature_stats[ci][0]["sleep"] == 0.0


def test_creature_move_prioritizes_sleep_over_feed_when_it_is_the_higher_need(world):
    ci = 0
    st = world.all_creature_stats[ci][0]
    st["hunger"] = 2  # feed need = 1
    st["sleep"] = 5.0  # sleep need = 5, dominant
    x, z = world.all_creature_positions[ci][0]

    result = world._creature_move(ci, 0, world.creature_defs[ci], x, z, avoids=set())

    assert result == (x, z)  # sleeping creatures don't move
    assert world.all_creature_stats[ci][0]["asleep"] is True


def test_creature_move_feeds_in_place_when_feed_need_is_dominant(world):
    ci = 0
    st = world.all_creature_stats[ci][0]
    x, z = world.all_creature_positions[ci][0]
    st["hunger"] = 0  # feed need = 3, dominant over sleep = 0
    st["sleep"] = 0.0
    world._spawn_drop("seed", 2, x, z)  # food right where the creature stands

    result = world._creature_move(ci, 0, world.creature_defs[ci], x, z, avoids=set())

    assert result == (x, z)  # ate in place rather than moving
    assert world.all_creature_stats[ci][0]["hunger"] > 0
    assert world.world_drops == []  # fully consumed (count=2, hunger gain=2)


def test_creature_move_random_walk_when_no_needs_are_active(world):
    ci = 0
    st = world.all_creature_stats[ci][0]
    st["hunger"] = 3  # feed need 0
    st["sleep"] = 0.0  # sleep need 0
    x, z = world.all_creature_positions[ci][0]

    nx, nz = world._creature_move(ci, 0, world.creature_defs[ci], x, z, avoids=set())

    assert abs(nx - x) + abs(nz - z) <= 1


# ── daily / seasonal lifecycle ────────────────────────────────────────────────

def test_on_day_start_decrements_hunger_before_touching_age(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 3
    world.all_creature_stats[ci][0]["age"] = 2

    world._on_day_start()

    assert world.all_creature_stats[ci][0]["hunger"] == 2
    assert world.all_creature_stats[ci][0]["age"] == 2  # unchanged


def test_on_day_start_decrements_age_once_hunger_is_already_zero(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["age"] = 2

    world._on_day_start()

    assert world.all_creature_stats[ci][0]["hunger"] == 0
    assert world.all_creature_stats[ci][0]["age"] == 1


def test_on_day_start_removes_creature_when_age_hits_zero(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 0
    world.all_creature_stats[ci][0]["age"] = 1
    before = len(world.all_creature_positions[ci])

    world._on_day_start()

    assert len(world.all_creature_positions[ci]) == before - 1


def test_on_day_start_wakes_sleeping_creatures_that_have_a_sleep_need(world):
    ci = 0
    world._sleep_creature(ci, 0, world.creature_defs[ci])
    world.all_creature_stats[ci][0]["sleep"] = 2.0

    world._on_day_start()

    assert world.all_creature_stats[ci][0]["asleep"] is False
    assert world.all_creature_stats[ci][0]["sleep"] == 0.0


def test_on_season_start_winter_ages_and_can_remove(world):
    # Single animal at age 1 dies on winter before breeding, so the
    # population ends at zero (no survivors to reproduce).
    ci = 0
    world.all_creature_positions[ci] = [(1, 1)]
    world.all_creature_stats[ci] = [{
        "id": 99, "age": 1, "hunger": 3, "attack": 5,
        "sleep": 0.0, "asleep": False,
    }]

    world._on_season_start("winter")

    assert len(world.all_creature_positions[ci]) == 0


def test_on_season_start_winter_survivor_still_reproduces(world):
    ci = 0
    world.all_creature_positions[ci] = [(2, 2)]
    world.all_creature_stats[ci] = [{
        "id": 100, "age": 2, "hunger": 3, "attack": 5,
        "sleep": 0.0, "asleep": False,
    }]

    world._on_season_start("winter")

    # Survives age tick (2 -> 1), then breeds once (reproduce_count [1, 1]).
    assert len(world.all_creature_positions[ci]) == 2
    assert world.all_creature_stats[ci][0]["age"] == 1


@pytest.mark.parametrize("season_name", ["spring", "summer", "fall"])
def test_on_season_start_reproduces_on_every_non_winter_season(world, season_name):
    ci = 0
    before = len(world.all_creature_positions[ci])

    world._on_season_start(season_name)

    # reproduce_count is [1, 1] in the fixture -> exactly one offspring/parent
    assert len(world.all_creature_positions[ci]) == before * 2


def test_spawn_creature_at_appends_a_new_instance_with_a_fresh_id(world):
    ci = 0
    before_ids = {st["id"] for st in world.all_creature_stats[ci]}

    world._spawn_creature_at(ci, 3, 3)

    assert world.all_creature_positions[ci][-1] == (3, 3)
    new_id = world.all_creature_stats[ci][-1]["id"]
    assert new_id not in before_ids


def test_remove_creature_drops_its_loot_and_shrinks_the_lists(world):
    ci = 0
    x, z = world.all_creature_positions[ci][0]
    before = len(world.all_creature_positions[ci])

    world._remove_creature(ci, 0)

    assert len(world.all_creature_positions[ci]) == before - 1
    assert any(d["item"] == "meat" and d["x"] == x and d["z"] == z for d in world.world_drops)


# ── flora simulation cycle (_sim_step) ───────────────────────────────────────

def test_sim_step_advances_cycle_and_rotates_season_at_the_configured_boundary(world):
    # season_length=2 in the fixture: cycle 1 stays spring, cycle 2 rotates on
    world._sim_step()
    assert world.current_cycle == 1
    assert world.current_season == "spring"

    world._sim_step()
    assert world.current_cycle == 2
    assert world.current_season == "summer"


def test_sim_step_decays_flower_to_death_and_drops_loot(world, monkeypatch):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 1
    world.chunk.fertility = 0
    world.chunk.moisture = 0  # guarantees the decay roll "kills" this tile

    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 100)
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)  # never spawn

    world._sim_step()

    assert world.chunk.get_block(1, world.SY, 1) == GRASS
    assert (1, 1) not in world.chunk.vegetation_ages
    assert any(d["item"] == "seed" for d in world.world_drops)
    assert world.vegetation_revision >= 1


def test_sim_step_decays_flower_age_without_killing_it(world, monkeypatch):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 2  # fresh; one decay tick from dry
    world.chunk.fertility = 0

    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 100)  # forces decay
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)  # never spawn

    world._sim_step()

    assert world.chunk.get_block(1, world.SY, 1) == FLOWER  # still alive
    assert world.chunk.vegetation_ages[(1, 1)] == 1  # aged down by one, not killed


def test_sim_step_spawns_new_flora_when_every_roll_is_forced_to_succeed(world, monkeypatch):
    world.chunk.fertility = 100  # guarantee the fertility gate passes
    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)  # spawn chance always hits

    world._sim_step()

    flower_count = sum(
        1
        for x in range(world.sx)
        for z in range(world.sz)
        if world.chunk.get_block(x, world.SY, z) == FLOWER
    )
    assert flower_count > 0


def test_sim_step_leaves_vegetation_revision_untouched_when_nothing_changes(world, monkeypatch):
    world.chunk.fertility = 0
    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 100)
    monkeypatch.setattr(server_module.random, "random", lambda: 1.0)
    before_rev = world.vegetation_revision

    world._sim_step()

    assert world.vegetation_revision == before_rev


def test_sim_step_blocks_bush_and_tree_spawn_when_a_tree_is_too_close(world, monkeypatch):
    # Fill the whole grid with TREE, except:
    #   (0, 0): the single GRASS candidate tile under test
    #   (0, 1), (1, 0): pre-placed FLOWER, so flower's own max_same_within(2)
    #                   cap is already met at (0, 0) and it gets skipped too.
    for x in range(world.sx):
        for z in range(world.sz):
            world.chunk.set_block(x, world.SY, z, TREE)
    world.chunk.set_block(0, world.SY, 0, GRASS)
    world.chunk.set_block(0, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(0, 1)] = 2
    world.chunk.set_block(1, world.SY, 0, FLOWER)
    world.chunk.vegetation_ages[(1, 0)] = 2

    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)  # always pass fertility/decay gates
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)  # always pass the "chance" roll

    world._sim_step()

    # flower/bush/tree are all blocked for (0, 0):
    #  - flower: max_same_within already met (2 flowers within radius 1)
    #  - bush/tree: requires_no_tree_within, and the grid is wall-to-wall TREE
    # grass has no proximity constraints, so it's free to claim the tile as
    # the lowest-priority fallback (tried last in flora_defs order)
    assert world.chunk.get_block(0, world.SY, 0) == GRASS_PATCH


def test_sim_step_spawns_bush_when_flower_is_capped_but_bush_is_unblocked(world, monkeypatch):
    world.chunk.set_block(2, world.SY, 3, FLOWER)
    world.chunk.vegetation_ages[(2, 3)] = 2
    world.chunk.set_block(3, world.SY, 2, FLOWER)
    world.chunk.vegetation_ages[(3, 2)] = 2
    # (3, 3) stays GRASS; it has 2 flower-neighbors within radius 1, hitting
    # flower's own max_same_within cap, so flower is skipped and bush -- the
    # next candidate in priority order -- gets a clean, unblocked attempt.

    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)

    world._sim_step()

    assert world.chunk.get_block(3, world.SY, 3) == BUSH


def test_sim_step_flower_can_spawn_on_top_of_a_grass_patch(world, monkeypatch):
    """Ground-cover is a soft layer: flower/bush/tree may claim a grass tile."""
    for x in range(world.sx):
        for z in range(world.sz):
            world.chunk.set_block(x, world.SY, z, GRASS_PATCH)
            world.chunk.vegetation_ages[(x, z)] = 5

    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.0)  # flower chance always hits

    world._sim_step()

    assert world.chunk.get_block(0, world.SY, 0) == FLOWER
    flower_count = sum(1 for x in range(world.sx) for z in range(world.sz)
                        if world.chunk.get_block(x, world.SY, z) == FLOWER)
    assert flower_count > 0


def test_sim_step_grass_still_cannot_spawn_on_an_existing_grass_patch(world, monkeypatch):
    world.chunk.set_block(0, world.SY, 0, GRASS_PATCH)
    world.chunk.vegetation_ages[(0, 0)] = 5
    # Leave the rest bare so we can force flower/bush/tree chance to fail
    # and prove grass does not re-roll onto the already-grassy tile.
    world.chunk.fertility = 100
    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)
    monkeypatch.setattr(server_module.random, "random", lambda: 0.10)  # only grass chance passes

    world._sim_step()

    assert world.chunk.get_block(0, world.SY, 0) == GRASS_PATCH


def test_sim_step_spawns_grass_on_bare_soil_when_season_is_active(world, monkeypatch):
    world.chunk.fertility = 100
    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)
    # 0.10 fails flower(0.09)/bush(0.03)/tree(0.01)'s chance rolls but passes
    # grass's (0.15) -- isolates the assertion to specifically grass's roll.
    monkeypatch.setattr(server_module.random, "random", lambda: 0.10)
    assert world.current_season == "spring"  # in grass's active_seasons

    world._sim_step()

    def count(bid):
        return sum(1 for x in range(world.sx) for z in range(world.sz)
                    if world.chunk.get_block(x, world.SY, z) == bid)

    assert count(GRASS_PATCH) > 0
    assert count(FLOWER) == 0  # confirms it was specifically grass's roll


def test_sim_step_does_not_spawn_grass_outside_its_active_seasons(world, monkeypatch):
    world._apply_season("winter")  # not in grass's active_seasons
    world.chunk.fertility = 100
    monkeypatch.setattr(server_module.random, "randint", lambda lo, hi: 0)
    # same roll that succeeded for grass above -- the season gate, not the
    # chance roll, is what must block it here
    monkeypatch.setattr(server_module.random, "random", lambda: 0.10)

    world._sim_step()

    grass_count = sum(1 for x in range(world.sx) for z in range(world.sz)
                       if world.chunk.get_block(x, world.SY, z) == GRASS_PATCH)
    assert grass_count == 0


# ── main tick() scheduling ────────────────────────────────────────────────────

def test_tick_detects_night_to_day_transition_and_calls_on_day_start(world, monkeypatch):
    world._prev_is_day = False
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)  # phase=0 -> is_day True
    called = {"day_start": False}
    original = world._on_day_start

    def spy():
        called["day_start"] = True
        original()

    monkeypatch.setattr(world, "_on_day_start", spy)
    before_day = world.current_day

    world.tick(dt=0.01)

    assert called["day_start"] is True
    assert world.current_day == before_day + 1
    assert world._prev_is_day is True


def test_tick_does_not_re_trigger_day_start_while_already_day(world, monkeypatch):
    world._prev_is_day = True
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    called = {"day_start": False}
    monkeypatch.setattr(world, "_on_day_start", lambda: called.__setitem__("day_start", True))
    before_day = world.current_day

    world.tick(dt=0.01)

    assert called["day_start"] is False
    assert world.current_day == before_day


def test_tick_increments_revision_exactly_once_per_call(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    before = world.revision

    world.tick(dt=0.01)
    assert world.revision == before + 1

    world.tick(dt=0.01)
    assert world.revision == before + 2


def test_tick_triggers_sim_step_only_after_cycle_length_elapses(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    assert world.current_cycle == 0

    world.tick(dt=world.cycle_length - 0.001)
    assert world.current_cycle == 0  # not yet

    world.tick(dt=0.01)  # crosses the threshold
    assert world.current_cycle == 1


def test_tick_triggers_a_periodic_save_only_after_save_interval_elapses(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    saved = {"count": 0}
    monkeypatch.setattr(world, "save", lambda: saved.__setitem__("count", saved["count"] + 1))

    world.tick(dt=world.save_interval - 0.001)
    assert saved["count"] == 0

    world.tick(dt=0.01)
    assert saved["count"] == 1


def test_tick_defers_creature_movement_evaluation_until_interval_elapses(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)  # is_day True
    ci = 0
    original_positions = list(world.all_creature_positions[ci])

    world.tick(dt=world.creature_defs[ci]["move_interval_day"] - 0.5)

    assert world.all_creature_positions[ci] == original_positions
    assert world._creature_timers[ci] > 0


def test_tick_resets_the_creature_timer_once_the_interval_fires(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 0.0)
    ci = 0
    interval = world.creature_defs[ci]["move_interval_day"]

    world.tick(dt=interval + 0.01)

    assert world._creature_timers[ci] == 0.0


def test_tick_accumulates_sleep_need_only_while_it_is_night(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 45.0)  # phase=0.75 -> night
    ci = 0
    interval = world.creature_defs[ci]["move_interval_day"]
    world.all_creature_stats[ci][0]["sleep"] = 0.0
    world.all_creature_stats[ci][0]["asleep"] = False

    world.tick(dt=interval + 0.01)

    assert world.all_creature_stats[ci][0]["sleep"] > 0.0


def test_tick_accumulates_timer_at_night_when_moves_at_night_is_enabled(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 45.0)  # night
    ci = 0
    world.creature_defs[ci]["moves_at_night"] = True

    world.tick(dt=1.0)

    assert world._creature_timers[ci] > 0


def test_tick_creature_timer_stays_zero_at_night_with_no_night_activity_enabled(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 45.0)  # night
    ci = 0
    world.creature_defs[ci]["moves_at_night"] = False
    world.creature_defs[ci]["needs"] = ["feed"]  # no "sleep" -> sleep_enabled False

    world.tick(dt=1.0)

    assert world._creature_timers[ci] == 0.0


def test_tick_skips_creature_move_for_every_sleeping_creature_at_night(world, monkeypatch):
    monkeypatch.setattr(server_module.time, "time", lambda: 45.0)  # night
    for stats in world.all_creature_stats:
        for st in stats:
            st["asleep"] = True  # every fauna instance asleep -> none should move
    original_positions = [list(p) for p in world.all_creature_positions]
    called = {"count": 0}
    original_move = world._creature_move

    def spy(*args, **kwargs):
        called["count"] += 1
        return original_move(*args, **kwargs)

    monkeypatch.setattr(world, "_creature_move", spy)
    interval = world.creature_defs[0]["move_interval_day"]

    world.tick(dt=interval + 0.01)

    assert called["count"] == 0
    assert [list(p) for p in world.all_creature_positions] == original_positions


# ── snapshot for API clients ──────────────────────────────────────────────────

def test_snapshot_includes_all_top_level_keys(world):
    snap = world.snapshot()
    for key in ("revision", "vegetation_revision", "chunk", "time", "terrain",
                "vegetation", "creatures", "drops"):
        assert key in snap


def test_snapshot_chunk_and_time_fields_reflect_world_state(world):
    snap = world.snapshot()
    assert snap["chunk"]["size"] == [world.sx, world.sy, world.sz]
    assert snap["chunk"]["surface_y"] == world.SY
    assert snap["time"]["season"] == world.current_season
    assert snap["time"]["day_night_cycle"] == world.day_night_cycle


def test_snapshot_vegetation_only_lists_known_flora_blocks_with_their_age(world):
    world.chunk.set_block(1, world.SY, 1, FLOWER)
    world.chunk.vegetation_ages[(1, 1)] = 2

    snap = world.snapshot()

    entries = [v for v in snap["vegetation"] if (v["x"], v["z"]) == (1, 1)]
    assert len(entries) == 1
    assert entries[0]["block_id"] == FLOWER
    assert entries[0]["type"] == "flower"
    assert entries[0]["age"] == 2


def test_snapshot_creatures_have_stable_ids_and_expected_fields(world):
    snap = world.snapshot()
    total = sum(len(p) for p in world.all_creature_positions)
    assert len(snap["creatures"]) == total
    c = next(c for c in snap["creatures"] if c["type"] == "rat")
    assert set(c.keys()) == {"id", "type", "x", "z", "age", "hunger", "sleep",
                             "asleep", "home", "carrying", "needs"}
    assert any(c["type"] == "rabbit" for c in snap["creatures"])


def test_snapshot_creatures_include_computed_needs(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 1
    world.all_creature_stats[ci][0]["sleep"] = 0.5
    world.all_creature_stats[ci][0]["asleep"] = False

    snap = world.snapshot()

    c = next(c for c in snap["creatures"] if c["id"] == world.all_creature_stats[ci][0]["id"])
    # feed is initial_hunger(3) - hunger(1); home is 0 until it accrues at day
    # start; stock is the constant from the definition
    assert c["needs"] == {"feed": 2, "sleep": 0.5, "home": 0.0, "stock": 0.9}


def test_snapshot_drops_include_a_computed_age_in_seconds(world):
    world._spawn_drop("seed", 1, 0, 0)
    world.world_drops[0]["spawn_time"] = time_module.time() - 2.5

    snap = world.snapshot()

    assert snap["drops"][0]["age"] >= 2.5
