"""Unit tests for server.py's World class -- the authoritative simulation
engine (config loading, seeding, ecology rules, creature AI/needs, daily and
seasonal lifecycle, the flora simulation cycle, tick() scheduling, and the
renderable snapshot)."""
import json
import os
import time as time_module

import pytest

import server as server_module
from chunk import BUSH, FLOWER, GRASS, GRASS_PATCH, TREE

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
    assert world._resolve_diet(cdef) == {"seed", "berry", "meat"}


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

def test_resolve_plant_diet_matches_vegetation_names_in_order(world):
    plants = world._resolve_plant_diet(world.creature_defs[1])
    assert [p["name"] for p in plants] == ["grass", "bush"]


def test_resolve_plant_diet_empty_for_item_only_diet(world):
    assert world._resolve_plant_diet(world.creature_defs[0]) == []


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


def test_pick_highest_need_selects_the_max_value(world):
    assert world._pick_highest_need({"feed": 2, "sleep": 0.5}) == "feed"
    assert world._pick_highest_need({"feed": 0.2, "sleep": 0.9}) == "sleep"


def test_pick_highest_need_none_when_all_zero_or_empty(world):
    assert world._pick_highest_need({"feed": 0, "sleep": 0}) is None
    assert world._pick_highest_need({}) is None


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
    assert set(c.keys()) == {"id", "type", "x", "z", "age", "hunger", "sleep", "asleep", "needs"}
    assert any(c["type"] == "rabbit" for c in snap["creatures"])


def test_snapshot_creatures_include_computed_needs(world):
    ci = 0
    world.all_creature_stats[ci][0]["hunger"] = 1
    world.all_creature_stats[ci][0]["sleep"] = 0.5
    world.all_creature_stats[ci][0]["asleep"] = False

    snap = world.snapshot()

    c = next(c for c in snap["creatures"] if c["id"] == world.all_creature_stats[ci][0]["id"])
    assert c["needs"] == {"feed": 2, "sleep": 0.5}  # initial_hunger(3) - hunger(1)


def test_snapshot_drops_include_a_computed_age_in_seconds(world):
    world._spawn_drop("seed", 1, 0, 0)
    world.world_drops[0]["spawn_time"] = time_module.time() - 2.5

    snap = world.snapshot()

    assert snap["drops"][0]["age"] >= 2.5
