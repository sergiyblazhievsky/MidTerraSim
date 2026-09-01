"""
Run once to generate the initial 100×100×100 grass chunk with random flowers.
Re-run any time you want to reset the world.
"""
import os
import random
from PIL import Image
from chunk import Chunk, AIR, GRASS, FLOWER, BUSH, TREE

# ── textures ──────────────────────────────────────────────────────────────────
os.makedirs("textures", exist_ok=True)

# grass — noisy green (spring/summer)
random.seed(42)
grass_img = Image.new("RGB", (16, 16))
for py in range(16):
    for px in range(16):
        grass_img.putpixel((px, py), (
            random.randint(65,  95),
            random.randint(115, 155),
            random.randint(40,  70),
        ))
grass_img.save("textures/grass.png")
print("Saved textures/grass.png")

# grass — yellowish fall
random.seed(43)
fall_img = Image.new("RGB", (16, 16))
for py in range(16):
    for px in range(16):
        fall_img.putpixel((px, py), (
            random.randint(155, 210),
            random.randint(138, 180),
            random.randint(55,  95),
        ))
fall_img.save("textures/grass_fall.png")
print("Saved textures/grass_fall.png")

# grass — pale winter
random.seed(44)
winter_img = Image.new("RGB", (16, 16))
for py in range(16):
    for px in range(16):
        winter_img.putpixel((px, py), (
            random.randint(210, 245),
            random.randint(220, 245),
            random.randint(220, 245),
        ))
winter_img.save("textures/grass_winter.png")
print("Saved textures/grass_winter.png")

# flower — red petals + yellow centre on transparent background
flower_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
# stem
for py in range(9, 16):
    flower_img.putpixel((7, py), (50, 140, 40, 255))
    flower_img.putpixel((8, py), (60, 150, 50, 255))
# petals (ring)
for py in range(1, 12):
    for px in range(1, 15):
        d = ((px - 7.5)**2 + (py - 6.0)**2) ** 0.5
        if 2.2 < d < 5.5:
            flower_img.putpixel((px, py), (220, 50, 80, 255))
# centre
for py in range(3, 10):
    for px in range(3, 13):
        d = ((px - 7.5)**2 + (py - 5.5)**2) ** 0.5
        if d < 2.5:
            flower_img.putpixel((px, py), (255, 200, 20, 255))
flower_img.save("textures/flower.png")
print("Saved textures/flower.png")

# dry flower — wilted petals + brown stem
flower_dry_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
for py in range(9, 16):
    flower_dry_img.putpixel((7, py), (110, 80, 40, 255))
    flower_dry_img.putpixel((8, py), (120, 90, 45, 255))
for py in range(1, 12):
    for px in range(1, 15):
        d = ((px - 7.5)**2 + (py - 6.0)**2) ** 0.5
        if 2.2 < d < 5.5:
            flower_dry_img.putpixel((px, py), (170, 120, 65, 255))
for py in range(3, 10):
    for px in range(3, 13):
        d = ((px - 7.5)**2 + (py - 5.5)**2) ** 0.5
        if d < 2.5:
            flower_dry_img.putpixel((px, py), (190, 150, 90, 255))
flower_dry_img.save("textures/flower_dry.png")
print("Saved textures/flower_dry.png")

# bush — generic green leaves on transparent background
bush_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
for py in range(16):
    for px in range(16):
        dx = (px - 7.5)
        dy = (py - 8.0)
        d = (dx*dx + dy*dy) ** 0.5
        if d < 6.0:
            value = max(0, 255 - int(d * 16))
            bush_img.putpixel((px, py), (35 + value // 3, 140 + value // 5, 55 + value // 8, 255))
for py in range(4, 16):
    for px in range(5, 11):
        if random.random() < 0.22:
            bush_img.putpixel((px, py), (60, 180, 60, 255))
bush_img.save("textures/bush.png")
print("Saved textures/bush.png")

# dry bush — brown/tan leaves and sparse stems
bush_dry_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
for py in range(16):
    for px in range(16):
        dx = (px - 7.5)
        dy = (py - 8.0)
        d = (dx*dx + dy*dy) ** 0.5
        if d < 6.0:
            value = max(0, 255 - int(d * 16))
            bush_dry_img.putpixel((px, py), (120 + value // 8, 90 + value // 10, 45 + value // 12, 255))
for py in range(4, 16):
    for px in range(5, 11):
        if random.random() < 0.20:
            bush_dry_img.putpixel((px, py), (110, 80, 40, 255))
for py in range(8, 16):
    for px in range(6, 10):
        if random.random() < 0.55:
            bush_dry_img.putpixel((px, py), (90, 70, 30, 255))
bush_dry_img.save("textures/bush_dry.png")
print("Saved textures/bush_dry.png")

# tree — leafy crown + trunk, visibly more tree-like than a shrub
random.seed(45)
tree_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))

# trunk
for py in range(8, 16):
    for px in range(6, 10):
        trunk_noise = random.random()
        if trunk_noise < 0.9:
            tree_img.putpixel((px, py), (110, 78, 42, 255))

# canopy layers
for py in range(0, 8):
    for px in range(0, 16):
        dx = (px - 7.5)
        dy = (py - 4.0)
        d = (dx*dx + dy*dy) ** 0.5
        if d < 5.2:
            shade = 160 - int(d * 12)
            tree_img.putpixel((px, py), (max(0, shade - 20), max(60, shade), max(0, shade - 60), 255))

# add darker patchy leaves for realism
for _ in range(90):
    px = random.randint(1, 14)
    py = random.randint(0, 7)
    if (px - 7.5) ** 2 + (py - 4.0) ** 2 < 26:
        tree_img.putpixel((px, py), (30, 130, 50, 255))

# small ground shadow
for py in range(13, 16):
    for px in range(3, 13):
        if abs(px - 8) + abs(py - 14) < 5:
            tree_img.putpixel((px, py), (80, 50, 30, 150))

tree_img.save("textures/tree.png")
print("Saved textures/tree.png")

# dead tree — bare trunk + dry brown crown
random.seed(46)
tree_dead_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
for py in range(8, 16):
    for px in range(6, 10):
        if random.random() < 0.9:
            tree_dead_img.putpixel((px, py), (90, 60, 30, 255))
for py in range(0, 8):
    for px in range(0, 16):
        dx = (px - 7.5)
        dy = (py - 4.0)
        d = (dx*dx + dy*dy) ** 0.5
        if d < 5.0:
            shade = 160 - int(d * 8)
            tree_dead_img.putpixel((px, py), (max(70, shade), max(70, shade - 10), max(20, shade - 30), 255))
for _ in range(70):
    px = random.randint(1, 14)
    py = random.randint(0, 7)
    if (px - 7.5) ** 2 + (py - 4.0) ** 2 < 24:
        tree_dead_img.putpixel((px, py), (120, 90, 45, 255))
for py in range(13, 16):
    for px in range(3, 13):
        if abs(px - 8) + abs(py - 14) < 5:
            tree_dead_img.putpixel((px, py), (80, 50, 30, 150))
tree_dead_img.save("textures/tree_dead.png")
print("Saved textures/tree_dead.png")

# rat — simple upright cardboard-cutout rat, tall rather than flat
random.seed(47)
rat_img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))

# body
for py in range(4, 13):
    for px in range(4, 12):
        if abs(px - 8) < 3 and abs(py - 8) < 4:
            rat_img.putpixel((px, py), (120, 100, 80, 255))

# head
for py in range(1, 6):
    for px in range(5, 11):
        if (px - 8) ** 2 + (py - 3) ** 2 < 16:
            rat_img.putpixel((px, py), (125, 105, 85, 255))

# ears
rat_img.putpixel((6, 1), (110, 90, 70, 255))
rat_img.putpixel((9, 1), (110, 90, 70, 255))

# snout / nose
rat_img.putpixel((8, 5), (90, 70, 55, 255))
rat_img.putpixel((7, 6), (90, 70, 55, 255))
rat_img.putpixel((9, 6), (90, 70, 55, 255))

# paws / legs
for px in range(5, 11, 2):
    rat_img.putpixel((px, 12), (100, 80, 65, 255))
    rat_img.putpixel((px, 13), (100, 80, 65, 255))

# tail
for py in range(5, 11):
    rat_img.putpixel((12, py), (85, 70, 55, 255))
    rat_img.putpixel((13, py), (85, 70, 55, 255))

# add slight shading to make it feel 3D / cardboard cutout
for py in range(4, 13):
    for px in range(4, 12):
        if abs(px - 8) < 2 and abs(py - 8) < 3:
            rat_img.putpixel((px, py), (145, 120, 95, 255))

rat_img.save("textures/rat.png")
print("Saved textures/rat.png")

# ── world ─────────────────────────────────────────────────────────────────────
os.makedirs("chunks", exist_ok=True)
chunk = Chunk(size=(100, 100, 100))
chunk.fill(GRASS)

random.seed(7)
sx, sy, sz = chunk.size
surface_y = sy - 1   # y = 99


def _count_kind(x0, z0, kind, radius):
    count = 0
    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            if chunk.get_block(x0 + dx, surface_y, z0 + dz) == kind:
                count += 1
    return count


def place_tree_if_possible(x, z):
    if chunk.get_block(x, surface_y, z) != GRASS:
        return False
    base_y = surface_y - 1
    if chunk.get_block(x, base_y, z) == AIR:
        return False
    if x < 2 or z < 2 or x >= sx - 2 or z >= sz - 2:
        return False
    if _count_kind(x, z, TREE, 2) >= 2:
        return False
    if _count_kind(x, z, BUSH, 1) > 0:
        return False
    chunk.set_block(x, surface_y, z, TREE)
    chunk.vegetation_ages[(x, z)] = 10
    return True


def place_bush_if_possible(x, z):
    if chunk.get_block(x, surface_y, z) != GRASS:
        return False
    base_y = surface_y - 1
    if chunk.get_block(x, base_y, z) == AIR:
        return False
    if x < 1 or z < 1 or x >= sx - 1 or z >= sz - 1:
        return False
    if _count_kind(x, z, BUSH, 1) >= 1:
        return False
    if _count_kind(x, z, TREE, 1) > 0:
        return False
    chunk.set_block(x, surface_y, z, BUSH)
    chunk.vegetation_ages[(x, z)] = 5
    return True


def place_flower_if_possible(x, z):
    if chunk.get_block(x, surface_y, z) != GRASS:
        return False
    base_y = surface_y - 1
    if chunk.get_block(x, base_y, z) == AIR:
        return False
    if _count_kind(x, z, TREE, 1) > 0:
        return False
    if _count_kind(x, z, BUSH, 1) > 0:
        return False
    if _count_kind(x, z, FLOWER, 1) >= 2:
        return False
    chunk.set_block(x, surface_y, z, FLOWER)
    chunk.vegetation_ages[(x, z)] = 2
    return True

for x in range(sx):
    for z in range(sz):
        if chunk.get_block(x, surface_y, z) != GRASS:
            continue
        base_y = surface_y - 1
        if chunk.get_block(x, base_y, z) == AIR:
            continue
        if random.random() < 0.010 and place_tree_if_possible(x, z):
            continue
        if random.random() < 0.030 and place_bush_if_possible(x, z):
            continue
        if random.random() < 0.070 and place_flower_if_possible(x, z):
            continue

chunk.save("chunks/chunk_0_0.wrld")
n_flowers = sum(1 for v in chunk._overrides.values() if v == FLOWER)
n_bushes = sum(1 for v in chunk._overrides.values() if v == BUSH)
n_trees = sum(1 for v in chunk._overrides.values() if v == TREE)
print(f"Saved chunks/chunk_0_0.wrld  ({n_flowers} flowers, {n_bushes} bushes, {n_trees} trees placed)")

