"""
slicer_core.py — Moteur de découpe 3D -> tranches SVG pour découpe laser.

Deux méthodes :
  - "stacked"  : couches parallèles empilées le long d'un axe (effet topographie).
  - "crossed"  : tranches en croix (X + Y) avec fentes d'emboîtement (egg-crate).

Le module est pur Python (trimesh + shapely + numpy) et ne dépend d'aucune
interface. Il est testable en headless : il produit de vrais fichiers SVG.

Convention : toutes les longueurs sont en millimètres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np
import trimesh
from shapely.geometry import Polygon, MultiPolygon, LineString, box, Point
from shapely.ops import unary_union
from shapely import affinity


# -----------------------------------------------------------------------------
# Paramètres
# -----------------------------------------------------------------------------

@dataclass
class SliceParams:
    """Tous les réglages de découpe."""
    method: str = "stacked"          # "stacked" | "crossed"
    axis: str = "z"                  # axe d'empilement pour "stacked" (x|y|z)
    n_slices: int = 12               # nb de tranches (stacked) ou par axe (crossed)
    n_slices_y: Optional[int] = None # crossed : nb de tranches sur le 2e axe (déf = n_slices)
    thickness: float = 3.0           # épaisseur du matériau (mm)
    kerf: float = 0.15               # largeur du trait laser (mm)
    fit: float = 0.05                # jeu d'ajustement des fentes (mm) : + = plus lâche
    target_size: Optional[float] = None  # redimensionner le modèle : plus grande dim -> mm
    sheet_w: float = 300.0           # largeur planche pour le nesting (mm)
    sheet_h: float = 200.0           # hauteur planche (mm)
    margin: float = 5.0              # marge entre pièces et bords (mm)
    dowel_holes: bool = False        # stacked : trous d'alignement pour tige
    dowel_dia: float = 4.0           # diamètre tige (mm)

    @property
    def slot_width(self) -> float:
        """Largeur *dessinée* d'une fente pour recevoir une pièce d'épaisseur `thickness`.
        Le laser élargit l'ouverture de ~kerf, on soustrait donc kerf et on ajoute le jeu."""
        return max(0.1, self.thickness - self.kerf + self.fit)


# -----------------------------------------------------------------------------
# Représentation d'une tranche
# -----------------------------------------------------------------------------

@dataclass
class Slice:
    """Une tranche découpée, en coordonnées locales 2D (u, v)."""
    polygon: Polygon | MultiPolygon   # contour(s) 2D dans le plan de la tranche
    axis: str                          # 'x' | 'y' | 'z' : normale du plan
    pos: float                         # position le long de l'axe (mm)
    index: int = 0                     # numéro d'ordre
    group: str = "A"                   # 'A' (empilé / X) ou 'B' (Y) pour l'étiquetage
    label: str = ""

    def to_3d_polylines(self) -> List[np.ndarray]:
        """Renvoie les contours 3D (liste de (n,3)) pour l'aperçu, placés dans l'espace."""
        polys = _as_polygon_list(self.polygon)
        out = []
        for p in polys:
            for ring in [p.exterior] + list(p.interiors):
                uv = np.asarray(ring.coords)
                out.append(self._uv_to_xyz(uv))
        return out

    def to_extruded_mesh(self) -> Optional[trimesh.Trimesh]:
        """Extrude la tranche de `thickness` pour l'aperçu 3D solide. Peut renvoyer None."""
        polys = _as_polygon_list(self.polygon)
        polys = [p for p in polys if p.area > 1e-6]
        if not polys:
            return None
        geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        try:
            m = trimesh.creation.extrude_polygon(geom, height=self._thickness)
        except Exception:
            return None
        # extrude_polygon crée l'objet dans le plan XY, extrudé en +Z, base en z=0.
        # On le replace dans l'orientation/position de la tranche.
        m.apply_transform(self._placement_matrix())
        return m

    # -- interne ------------------------------------------------------------
    _thickness: float = 3.0

    def _uv_to_xyz(self, uv: np.ndarray) -> np.ndarray:
        n = uv.shape[0]
        xyz = np.zeros((n, 3))
        u, v = uv[:, 0], uv[:, 1]
        if self.axis == "x":       # plan de normale X : local (u=Y, v=Z)
            xyz[:, 0] = self.pos; xyz[:, 1] = u; xyz[:, 2] = v
        elif self.axis == "y":     # normale Y : local (u=X, v=Z)
            xyz[:, 0] = u; xyz[:, 1] = self.pos; xyz[:, 2] = v
        else:                       # normale Z : local (u=X, v=Y)
            xyz[:, 0] = u; xyz[:, 1] = v; xyz[:, 2] = self.pos
        return xyz

    def _placement_matrix(self) -> np.ndarray:
        """Matrice 4x4 : repère local (extrusion en +Z depuis z=0) -> monde."""
        t = self._thickness
        M = np.eye(4)
        if self.axis == "x":
            # local x->world y, local y->world z, extrusion z->world x
            M[:3, 0] = [0, 1, 0]
            M[:3, 1] = [0, 0, 1]
            M[:3, 2] = [1, 0, 0]
            M[:3, 3] = [self.pos - t / 2.0, 0, 0]
        elif self.axis == "y":
            M[:3, 0] = [1, 0, 0]
            M[:3, 1] = [0, 0, 1]
            M[:3, 2] = [0, 1, 0]
            M[:3, 3] = [0, self.pos - t / 2.0, 0]
        else:
            M[:3, 0] = [1, 0, 0]
            M[:3, 1] = [0, 1, 0]
            M[:3, 2] = [0, 0, 1]
            M[:3, 3] = [0, 0, self.pos - t / 2.0]
        return M


# -----------------------------------------------------------------------------
# Chargement / préparation du maillage
# -----------------------------------------------------------------------------

def load_mesh(path: str) -> trimesh.Trimesh:
    """Charge un STL/OBJ/PLY... et renvoie un unique Trimesh."""
    m = trimesh.load(path, force="mesh")
    if isinstance(m, trimesh.Scene):
        m = m.dump(concatenate=True)
    if not isinstance(m, trimesh.Trimesh):
        raise ValueError("Le fichier ne contient pas de maillage exploitable.")
    return m


def prepare_mesh(mesh: trimesh.Trimesh, params: SliceParams) -> trimesh.Trimesh:
    """Centre le modèle et le redimensionne éventuellement. Ne modifie pas l'original."""
    m = mesh.copy()
    m.apply_translation(-m.bounds.mean(axis=0))  # centre sur l'origine
    if params.target_size:
        cur = float(m.extents.max())
        if cur > 1e-9:
            m.apply_scale(params.target_size / cur)
    return m


# -----------------------------------------------------------------------------
# Section d'un maillage par un plan -> polygone shapely 2D
# -----------------------------------------------------------------------------

_AXIS_NORMAL = {"x": [1.0, 0, 0], "y": [0, 1.0, 0], "z": [0, 0, 1.0]}
_AXIS_COLS = {"x": (1, 2), "y": (0, 2), "z": (0, 1)}  # colonnes (u, v) du point 3D


def section_polygon(mesh: trimesh.Trimesh, axis: str, pos: float) -> Optional[Polygon | MultiPolygon]:
    """Coupe le maillage par le plan {axis = pos} et renvoie le polygone 2D local (u, v).
    Les axes locaux sont fixés (voir _AXIS_COLS) pour un placement 3D prévisible."""
    origin = [0.0, 0.0, 0.0]
    origin["xyz".index(axis)] = pos
    try:
        path = mesh.section(plane_origin=origin, plane_normal=_AXIS_NORMAL[axis])
    except Exception:
        path = None
    if path is None:
        return None

    cu, cv = _AXIS_COLS[axis]
    loops = []
    for poly3d in path.discrete:          # liste de polylignes fermées (n,3)
        if len(poly3d) < 3:
            continue
        loops.append(np.column_stack([poly3d[:, cu], poly3d[:, cv]]))
    if not loops:
        return None
    return _loops_to_polygon(loops)


def _loops_to_polygon(loops: Sequence[np.ndarray]) -> Optional[Polygon | MultiPolygon]:
    """Assemble des boucles fermées en polygone(s), en gérant les trous (règle even-odd)."""
    raw = []
    for loop in loops:
        try:
            p = Polygon(loop)
            if not p.is_valid:
                p = p.buffer(0)
            if p.area > 1e-9:
                raw.append(p)
        except Exception:
            continue
    if not raw:
        return None
    raw.sort(key=lambda p: p.area, reverse=True)
    result = None
    for p in raw:
        pt = p.representative_point()
        # profondeur d'imbrication : nb de polygones plus grands qui contiennent p
        depth = sum(1 for q in raw if q is not p and q.area > p.area and q.contains(pt))
        if depth % 2 == 0:
            result = p if result is None else result.union(p)
        else:
            result = p if result is None else result.difference(p)
    if result is None or result.is_empty:
        return None
    return result


# -----------------------------------------------------------------------------
# Méthode "empilée"
# -----------------------------------------------------------------------------

def slice_stacked(mesh: trimesh.Trimesh, params: SliceParams) -> List[Slice]:
    axis = params.axis
    lo, hi = mesh.bounds[0]["xyz".index(axis)], mesh.bounds[1]["xyz".index(axis)]
    t = params.thickness
    # Empilement contigu : une tranche par épaisseur de matériau, prise au milieu de la couche.
    total = hi - lo
    n = max(1, int(round(total / t))) if params.n_slices in (None, 0) else params.n_slices
    positions = np.linspace(lo + total / (2 * n), hi - total / (2 * n), n)

    slices: List[Slice] = []
    for i, pos in enumerate(positions):
        poly = section_polygon(mesh, axis, float(pos))
        if poly is None or poly.is_empty:
            continue
        if params.dowel_holes:
            poly = _add_dowel_holes(poly, params)
        s = Slice(polygon=poly, axis=axis, pos=float(pos), index=i, group="A",
                  label=f"A{i+1:02d}")
        s._thickness = t
        slices.append(s)
    return slices


def _add_dowel_holes(poly, params: SliceParams):
    minx, miny, maxx, maxy = poly.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    off = min(maxx - minx, maxy - miny) * 0.25
    r = (params.dowel_dia - params.kerf) / 2.0
    holes = [Point(cx - off, cy).buffer(r), Point(cx + off, cy).buffer(r)]
    for h in holes:
        if poly.contains(h):
            poly = poly.difference(h)
    return poly


# -----------------------------------------------------------------------------
# Méthode "croisée" (egg-crate) avec fentes d'emboîtement
# -----------------------------------------------------------------------------

def slice_crossed(mesh: trimesh.Trimesh, params: SliceParams) -> List[Slice]:
    """Tranches selon X (groupe A) et Y (groupe B). Les A sont fendues par le haut,
    les B par le bas ; elles se glissent verticalement l'une dans l'autre."""
    nx = params.n_slices
    ny = params.n_slices_y or params.n_slices
    bx = mesh.bounds
    xlo, xhi = bx[0][0], bx[1][0]
    ylo, yhi = bx[0][1], bx[1][1]
    xs = _interior_positions(xlo, xhi, nx)
    ys = _interior_positions(ylo, yhi, ny)

    # Sections brutes
    x_polys = {x: section_polygon(mesh, "x", x) for x in xs}
    y_polys = {y: section_polygon(mesh, "y", y) for y in ys}
    x_polys = {k: v for k, v in x_polys.items() if v and not v.is_empty}
    y_polys = {k: v for k, v in y_polys.items() if v and not v.is_empty}

    w = params.slot_width

    # Fentes dans les tranches X (locales : u=Y, v=Z) : encoche par le HAUT à u=y
    for x, poly in list(x_polys.items()):
        for y in y_polys:
            iv = _vertical_interval(poly, y)
            if iv is None:
                continue
            zc = 0.5 * (iv[0] + iv[1])
            slot = box(y - w / 2, zc, y + w / 2, iv[1] + 1.0)  # du milieu vers le haut
            poly = poly.difference(slot)
        x_polys[x] = poly

    # Fentes dans les tranches Y (locales : u=X, v=Z) : encoche par le BAS à u=x
    for y, poly in list(y_polys.items()):
        for x in x_polys:
            iv = _vertical_interval(poly, x)
            if iv is None:
                continue
            zc = 0.5 * (iv[0] + iv[1])
            slot = box(x - w / 2, iv[0] - 1.0, x + w / 2, zc)   # du bas vers le milieu
            poly = poly.difference(slot)
        y_polys[y] = poly

    slices: List[Slice] = []
    for i, x in enumerate(sorted(x_polys)):
        s = Slice(polygon=x_polys[x], axis="x", pos=float(x), index=i, group="A",
                  label=f"A{i+1:02d}")
        s._thickness = params.thickness
        slices.append(s)
    for j, y in enumerate(sorted(y_polys)):
        s = Slice(polygon=y_polys[y], axis="y", pos=float(y), index=j, group="B",
                  label=f"B{j+1:02d}")
        s._thickness = params.thickness
        slices.append(s)
    return slices


def _interior_positions(lo: float, hi: float, n: int) -> List[float]:
    """n positions réparties à l'intérieur de [lo, hi], sans toucher les bords."""
    n = max(1, n)
    step = (hi - lo) / (n + 1)
    return [lo + step * (k + 1) for k in range(n)]


def _vertical_interval(poly, u: float) -> Optional[Tuple[float, float]]:
    """Intervalle [vmin, vmax] où la colonne verticale à `u` traverse le polygone.
    Prend le plus long segment si la coupe est multiple (forme concave)."""
    minx, miny, maxx, maxy = poly.bounds
    if u <= minx or u >= maxx:
        return None
    line = LineString([(u, miny - 1.0), (u, maxy + 1.0)])
    inter = poly.intersection(line)
    if inter.is_empty:
        return None
    segs = []
    if inter.geom_type == "LineString":
        segs = [inter]
    elif inter.geom_type == "MultiLineString":
        segs = list(inter.geoms)
    elif inter.geom_type == "GeometryCollection":
        segs = [g for g in inter.geoms if g.geom_type == "LineString"]
    if not segs:
        return None
    best = max(segs, key=lambda s: s.length)
    ys = [c[1] for c in best.coords]
    return (min(ys), max(ys))


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------

def generate_slices(mesh: trimesh.Trimesh, params: SliceParams) -> List[Slice]:
    prepared = prepare_mesh(mesh, params)
    if params.method == "crossed":
        return slice_crossed(prepared, params)
    return slice_stacked(prepared, params)


# -----------------------------------------------------------------------------
# Nesting + export SVG
# -----------------------------------------------------------------------------

def _as_polygon_list(geom) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for g in geom.geoms if isinstance(g, Polygon)]
    return []


def _poly_to_svg_path(poly: Polygon, dx: float, dy: float) -> str:
    def ring(coords):
        pts = [f"{x + dx:.3f},{y + dy:.3f}" for x, y in coords]
        return "M" + " L".join(pts) + " Z"
    d = ring(poly.exterior.coords)
    for interior in poly.interiors:
        d += " " + ring(interior.coords)
    return d


def nest_slices(slices: List[Slice], params: SliceParams):
    """Range chaque tranche sur des planches (nesting « étagère » simple).
    Renvoie une liste de planches, chacune = liste de (slice, dx, dy)."""
    m = params.margin
    items = []
    for s in slices:
        for p in _as_polygon_list(s.polygon):
            if p.area < 1e-6:
                continue
            minx, miny, maxx, maxy = p.bounds
            # normalise le polygone à l'origine
            p2 = affinity.translate(p, -minx, -miny)
            items.append((s, p2, maxx - minx, maxy - miny))
    # grandes pièces d'abord (par hauteur)
    items.sort(key=lambda it: it[3], reverse=True)

    sheets: List[list] = []
    cur: list = []
    x = m; y = m; row_h = 0.0
    for s, p2, w, h in items:
        if w > params.sheet_w - 2 * m:
            pass  # trop large : on la pose quand même, l'utilisateur ajustera l'échelle
        if x + w + m > params.sheet_w:      # saut de ligne
            x = m; y += row_h + m; row_h = 0.0
        if y + h + m > params.sheet_h:      # saut de planche
            sheets.append(cur); cur = []
            x = m; y = m; row_h = 0.0
        cur.append((s, p2, x, y))
        x += w + m
        row_h = max(row_h, h)
    if cur:
        sheets.append(cur)
    return sheets


def export_svg(slices: List[Slice], params: SliceParams, out_prefix: str) -> List[str]:
    """Écrit une ou plusieurs planches SVG. Renvoie la liste des chemins créés.
    Traits de coupe en noir (0.05mm), étiquettes en bleu (gravure/repère)."""
    sheets = nest_slices(slices, params)
    paths = []
    for si, sheet in enumerate(sheets):
        w, h = params.sheet_w, params.sheet_h
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="white"/>',
        ]
        for s, p2, dx, dy in sheet:
            d = _poly_to_svg_path(p2, dx, dy)
            parts.append(
                f'<path d="{d}" fill="none" stroke="black" stroke-width="0.05"/>'
            )
            minx, miny, maxx, maxy = p2.bounds
            lx, ly = dx + (minx + maxx) / 2, dy + (miny + maxy) / 2
            parts.append(
                f'<text x="{lx:.2f}" y="{ly:.2f}" font-size="4" fill="blue" '
                f'text-anchor="middle" dominant-baseline="middle">{s.label}</text>'
            )
        parts.append("</svg>")
        path = f"{out_prefix}_planche{si+1}.svg"
        with open(path, "w") as f:
            f.write("\n".join(parts))
        paths.append(path)
    return paths


def export_calibration_svg(params: SliceParams, out_path: str) -> str:
    """Petit gabarit de calibration : une languette d'épaisseur `thickness` +
    une série de fentes de jeu croissant, pour trouver le bon ajustement sur SA machine."""
    fits = [-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15]
    t = params.thickness
    pad = 6.0
    slot_len = 14.0
    cellw = 12.0
    W = pad * 2 + cellw * len(fits)
    H = pad * 3 + slot_len + 10
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" '
        f'viewBox="0 0 {W} {H}">',
        f'<rect x="0" y="0" width="{W}" height="{H}" fill="white"/>',
        # contour du gabarit
        f'<rect x="1" y="1" width="{W-2}" height="{H-2}" fill="none" stroke="black" stroke-width="0.05"/>',
    ]
    y0 = pad + 8
    for i, fit in enumerate(fits):
        cx = pad + cellw * i + cellw / 2
        sw = max(0.1, t - params.kerf + fit)
        parts.append(
            f'<rect x="{cx - sw/2:.3f}" y="{y0:.3f}" width="{sw:.3f}" height="{slot_len:.3f}" '
            f'fill="none" stroke="black" stroke-width="0.05"/>'
        )
        parts.append(
            f'<text x="{cx:.2f}" y="{y0 - 2:.2f}" font-size="3" fill="blue" '
            f'text-anchor="middle">{fit:+.2f}</text>'
        )
    parts.append(
        f'<text x="{W/2:.2f}" y="{H - 4:.2f}" font-size="3.2" fill="blue" '
        f'text-anchor="middle">calibration jeu (mm) — ep={t} kerf={params.kerf}</text>'
    )
    parts.append("</svg>")
    with open(out_path, "w") as f:
        f.write("\n".join(parts))
    return out_path


# -----------------------------------------------------------------------------
# Aperçu : maillage assemblé / éclaté
# -----------------------------------------------------------------------------

def assembled_meshes(slices: List[Slice], explode: float = 0.0):
    """Renvoie une liste de (Trimesh, group) pour l'aperçu 3D des tranches.
    `explode` (0..~2) écarte les tranches le long de leur normale."""
    out = []
    for s in slices:
        m = s.to_extruded_mesh()
        if m is None:
            continue
        if explode:
            span = 1.0
            offset = s.pos * explode
            vec = np.array(_AXIS_NORMAL[s.axis]) * offset
            m.apply_translation(vec)
        out.append((m, s.group))
    return out
