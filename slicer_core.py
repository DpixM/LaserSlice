"""
slicer_core.py — Moteur de découpe 3D -> tranches SVG pour découpe laser.

Deux méthodes :
  - "stacked"  : couches parallèles empilées le long d'un axe (effet topographie).
  - "skeleton" : 1+ colonne(s) vertébrale(s) + côtes, avec joints à mi-bois
                 (comme les puzzles bois 3D : ça tient sans colle).

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
    method: str = "stacked"          # "stacked" | "skeleton"
    axis: str = "z"                  # axe d'empilement pour "stacked" (x|y|z)
    n_slices: int = 12               # nb de tranches (stacked) ou de côtes (skeleton)
    n_slices_y: Optional[int] = None # skeleton : nb de colonnes vertébrales (déf = 1)
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
# Extrusion robuste (l'aperçu 3D ne doit jamais faire disparaître une pièce)
# -----------------------------------------------------------------------------

def _extrude_safe(geom, height: float):
    """Extrude un polygone en volume. La triangulation peut échouer selon la
    version des libs quand le contour a des parois fines (ex. colonne à encoches)
    -> on retente avec plusieurs nettoyages du contour avant d'abandonner."""
    ops = [
        lambda g: g,
        lambda g: g.buffer(0),
        lambda g: g.simplify(0.02).buffer(0),
        lambda g: g.buffer(-0.03).buffer(0.05),   # arrondit/épaissit les parois fines
    ]
    for op in ops:
        try:
            g = op(geom)
        except Exception:
            continue
        if g is None or g.is_empty:
            continue
        try:
            m = trimesh.creation.extrude_polygon(g, height=height)
        except Exception:
            m = None
        if m is not None and len(m.faces) > 0:
            return m
    return None


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
        m = _extrude_safe(geom, self._thickness)
        if m is None:
            return None
        # extrude_polygon crée l'objet dans le plan XY, extrudé en +Z, base en z=0.
        # On le replace dans l'orientation/position de la tranche.
        m.apply_transform(self._placement_matrix())
        # La matrice des colonnes (axe Y) est une réflexion -> elle retourne les
        # normales, ce qui rendait la pièce quasi noire (invisible) à l'affichage.
        # On remet les normales cohérentes et vers l'extérieur.
        try:
            m.fix_normals()
        except Exception:
            pass
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


def _clean_poly(geom):
    """Nettoie une géométrie après les découpes : supprime les micro-fentes /
    slivers de largeur ~0 laissés par les opérations booléennes en flottant."""
    if geom is None or getattr(geom, "is_empty", True):
        return geom
    try:
        from shapely import set_precision
        geom = set_precision(geom, 1e-3)      # arrondit à 1 µm : tue les slivers
    except Exception:
        pass
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


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

    # Fixations : positions de trous FIXES (identiques pour toutes les tranches)
    # -> une tige verticale traverse toutes les couches et les maintient alignées.
    centers = _stacked_hole_centers(mesh, params) if params.dowel_holes else []

    slices: List[Slice] = []
    for i, pos in enumerate(positions):
        poly = section_polygon(mesh, axis, float(pos))
        if poly is None or poly.is_empty:
            continue
        if centers:
            poly = _add_dowel_holes(poly, params, centers)
        s = Slice(polygon=poly, axis=axis, pos=float(pos), index=i, group="A",
                  label=f"A{i+1:02d}")
        s._thickness = t
        slices.append(s)
    return slices


def _stacked_hole_centers(mesh: trimesh.Trimesh, params: SliceParams) -> List[Tuple[float, float]]:
    """Positions (u, v) des trous de tige, dans le plan perpendiculaire à l'axe
    d'empilement. Le modèle étant centré sur l'origine, ces positions sont les
    mêmes pour toutes les tranches -> la tige traverse toute la pile bien droite."""
    cu, _cv = _AXIS_COLS[params.axis]
    half_u = float(mesh.extents[cu]) / 2.0
    d = 0.45 * half_u
    return [(-d, 0.0), (d, 0.0)]


def _add_dowel_holes(poly, params: SliceParams, centers: Sequence[Tuple[float, float]]):
    """Perce des trous de tige aux positions `centers` (communes à toutes les
    tranches). On ne perce que là où la matière existe réellement."""
    r = max(0.3, (params.dowel_dia - params.kerf) / 2.0)
    for cx, cy in centers:
        h = Point(cx, cy).buffer(r)
        if poly.contains(h):
            poly = poly.difference(h)
    return poly


# -----------------------------------------------------------------------------
# Méthode "squelette" : 1+ colonne(s) vertébrale(s) + côtes (joints mi-bois)
# -----------------------------------------------------------------------------

def slice_skeleton(mesh: trimesh.Trimesh, params: SliceParams) -> List[Slice]:
    """Construit un squelette comme les vrais puzzles bois 3D :

      - COLONNE(S) : une (ou plusieurs) planche(s) verticale(s) le long du plus
        grand axe du corps ; elles portent le profil du modèle. Fendues par le HAUT.
      - CÔTES : des sections perpendiculaires réparties sur la longueur ; elles
        donnent le volume. Fendues par le BAS.

    À chaque croisement, colonne et côte se glissent l'une dans l'autre (joint à
    mi-bois) : les fentes se rejoignent à mi-hauteur, ça tient sans colle.
    """
    ext = mesh.extents
    # Grand axe horizontal = longueur du corps ; l'autre horizontal = largeur.
    length_axis = "x" if ext[0] >= ext[1] else "y"
    width_axis = "y" if length_axis == "x" else "x"
    li = "xyz".index(length_axis)
    wi = "xyz".index(width_axis)
    bx = mesh.bounds

    n_ribs = max(1, params.n_slices)
    n_spines = max(1, params.n_slices_y or 1)

    # Sécurité anti-chevauchement : si on demande trop de pièces pour la taille du
    # modèle, les encoches fusionnent et la pièce part en bouillie. On limite donc
    # le nombre pour garder un espacement >= 1.8 × la largeur de fente.
    min_gap = max(params.slot_width + 0.6, 1e-6)   # largeur de fente + paroi mini 0,6 mm

    def _fit(span):
        # espacement des pièces = span / (n + 1) ; on veut >= min_gap
        return max(1, int(span / min_gap - 1 + 1e-9))

    n_ribs = max(1, min(n_ribs, _fit(bx[1][li] - bx[0][li])))
    if n_spines > 1:
        n_spines = max(1, min(n_spines, _fit(bx[1][wi] - bx[0][wi])))

    rib_pos = _interior_positions(bx[0][li], bx[1][li], n_ribs)
    if n_spines == 1:
        spine_pos = [0.0]                                   # colonne centrale
    else:
        spine_pos = _interior_positions(bx[0][wi], bx[1][wi], n_spines)

    # Sections brutes (on jette les vides)
    spines = {s: section_polygon(mesh, width_axis, s) for s in spine_pos}
    ribs = {r: section_polygon(mesh, length_axis, r) for r in rib_pos}
    spines = {k: v for k, v in spines.items() if v and not v.is_empty}
    ribs = {k: v for k, v in ribs.items() if v and not v.is_empty}

    w = params.slot_width

    # Colonnes (local u = longueur, v = hauteur) : encoche par le HAUT à chaque côte.
    for sp, poly in list(spines.items()):
        for rp in ribs:
            iv = _vertical_interval(poly, rp)
            if iv is None:
                continue
            zc = 0.5 * (iv[0] + iv[1])
            poly = poly.difference(box(rp - w / 2, zc, rp + w / 2, iv[1] + 1.0))
        spines[sp] = _clean_poly(poly)

    # Côtes (local u = largeur, v = hauteur) : encoche par le BAS à chaque colonne.
    for rp, poly in list(ribs.items()):
        for sp in spines:
            iv = _vertical_interval(poly, sp)
            if iv is None:
                continue
            zc = 0.5 * (iv[0] + iv[1])
            poly = poly.difference(box(sp - w / 2, iv[0] - 1.0, sp + w / 2, zc))
        ribs[rp] = _clean_poly(poly)

    slices: List[Slice] = []
    for i, sp in enumerate(sorted(spines)):
        s = Slice(polygon=spines[sp], axis=width_axis, pos=float(sp), index=i,
                  group="A", label=f"C{i+1:02d}")          # C = Colonne
        s._thickness = params.thickness
        slices.append(s)
    for j, rp in enumerate(sorted(ribs)):
        s = Slice(polygon=ribs[rp], axis=length_axis, pos=float(rp), index=j,
                  group="B", label=f"R{j+1:02d}")          # R = côte (Rib)
        s._thickness = params.thickness
        slices.append(s)
    return slices


def max_skeleton_ribs(mesh: trimesh.Trimesh, params: SliceParams) -> int:
    """Nombre maximum de côtes qui tiennent le long du corps sans que les
    encoches de la colonne se chevauchent (selon épaisseur/kerf/jeu et taille)."""
    prepared = prepare_mesh(mesh, params)
    ext = prepared.extents
    length_axis = "x" if ext[0] >= ext[1] else "y"
    li = "xyz".index(length_axis)
    span = float(prepared.bounds[1][li] - prepared.bounds[0][li])
    min_gap = max(params.slot_width + 0.6, 1e-6)
    return max(1, int(span / min_gap - 1 + 1e-9))


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
    if params.method == "skeleton":
        return slice_skeleton(prepared, params)
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
    """Renvoie une liste de (Trimesh, group) pour l'aperçu 3D des pièces.
    `explode` écarte chaque pièce le long de sa normale. La colonne centrale
    (pos = 0) reste en place, les côtes s'en écartent : on voit bien le montage."""
    out = []
    for s in slices:
        m = s.to_extruded_mesh()
        if m is None:
            continue
        if explode:
            m.apply_translation(np.array(_AXIS_NORMAL[s.axis], dtype=float) * (s.pos * explode))
        out.append((m, s.group))
    return out
