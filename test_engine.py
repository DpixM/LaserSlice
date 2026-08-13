import os
import numpy as np
import trimesh
import cairosvg

import slicer_core as sc

os.makedirs("out", exist_ok=True)

def render(svg_path, png_path, scale=3):
    cairosvg.svg2png(url=svg_path, write_to=png_path, scale=scale, background_color="white")

# --- maillages de test -------------------------------------------------------
sphere = trimesh.creation.icosphere(subdivisions=3, radius=40)
# une forme moins triviale : un "bonhomme" = union sphere + cylindre allongé
cyl = trimesh.creation.cylinder(radius=15, height=90)
cyl.apply_translation([0, 0, 0])
blob = trimesh.util.concatenate([sphere, cyl])

torus = trimesh.creation.torus(major_radius=40, minor_radius=15)

tests = {"sphere": sphere, "blob": blob, "torus": torus}

for name, mesh in tests.items():
    print(f"\n=== {name} : bounds extents = {mesh.extents.round(1)} ===")

    # ---- stacked ----
    p = sc.SliceParams(method="stacked", axis="z", n_slices=10, thickness=3.0,
                       kerf=0.15, fit=0.05, sheet_w=300, sheet_h=200)
    sl = sc.generate_slices(mesh, p)
    files = sc.export_svg(sl, p, f"out/{name}_stacked")
    print(f"  stacked : {len(sl)} tranches, {len(files)} planche(s)")
    for f in files:
        render(f, f.replace(".svg", ".png"))

    # ---- crossed ----
    p2 = sc.SliceParams(method="crossed", n_slices=6, n_slices_y=6, thickness=3.0,
                        kerf=0.15, fit=0.05, sheet_w=300, sheet_h=200)
    sl2 = sc.generate_slices(mesh, p2)
    files2 = sc.export_svg(sl2, p2, f"out/{name}_crossed")
    print(f"  crossed : {len(sl2)} tranches (A+B), {len(files2)} planche(s)")
    for f in files2:
        render(f, f.replace(".svg", ".png"))

    # verif : combien de tranches croisées ont effectivement des fentes (trous)
    notched = 0
    for s in sl2:
        for poly in sc._as_polygon_list(s.polygon):
            # une fente ouverte sur le bord réduit l'aire ou crée des concavités;
            # on compte simplement les tranches non convexes
            if poly.area < poly.convex_hull.area - 1.0:
                notched += 1
                break
    print(f"  crossed : {notched}/{len(sl2)} tranches présentent des encoches")

# calibration
pc = sc.SliceParams(thickness=3.0, kerf=0.15)
sc.export_calibration_svg(pc, "out/calibration.svg")
render("out/calibration.svg", "out/calibration.png", scale=6)
print("\ncalibration OK")

# aperçu assemblé (juste vérifier que l'extrusion marche)
p2 = sc.SliceParams(method="crossed", n_slices=6, thickness=3.0)
sl2 = sc.generate_slices(blob, p2)
ms = sc.assembled_meshes(sl2, explode=0.0)
print(f"aperçu assemblé blob : {len(ms)} mailles extrudées")
tot = trimesh.util.concatenate([m for m, g in ms]) if ms else None
if tot is not None:
    print("  bounds assemblage:", tot.extents.round(1))
print("\nTOUS LES TESTS PASSÉS")
