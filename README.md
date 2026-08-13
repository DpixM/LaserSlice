# LaserSlice — modèle 3D → tranches SVG pour découpe laser

Petite appli bureau : tu importes un modèle 3D (STL/OBJ/PLY/GLB…), l'appli le
« tranche » et génère les **fichiers SVG** à découper au laser, avec les
**encoches d'emboîtement** et la **numérotation** pour savoir quelle pièce va où.

Deux méthodes de découpe :

- **Tranches empilées** — couches parallèles à empiler (effet topographie).
  Option : trous d'alignement pour une tige.
- **Emboîtement en croix (egg-crate)** — tranches dans deux directions (X et Y)
  avec des fentes qui se glissent l'une dans l'autre, comme les animaux en
  contreplaqué. Le groupe A est fendu par le haut, le groupe B par le bas ;
  les fentes se rejoignent exactement à mi-hauteur car les deux tranches
  traversent la même ligne verticale du modèle.

## Aperçu en direct

- **Vue 3D** : le modèle d'origine en semi-transparent (« ghost ») avec les
  tranches solides par-dessus. Le ghost sert de contrôle qualité : si tu mets
  trop peu de tranches, tu vois le modèle « dépasser ». Un curseur **Éclaté**
  écarte les pièces pour comprendre l'assemblage.
- **Vue Planches (2D)** : les pièces mises à plat, nestées sur les planches,
  numérotées — exactement ce qui sera découpé.

## Réglages qui comptent pour un bon emboîtement

- **Épaisseur matière** : l'épaisseur réelle de ton contreplaqué/MDF (mesure au
  pied à coulisse, un « 3 mm » fait souvent 2.8–3.2).
- **Kerf** : la largeur du trait que ton laser brûle (~0.1–0.3 mm). La largeur
  des fentes est calculée comme `épaisseur − kerf + jeu`.
- **Jeu d'ajustement** : + = plus lâche, − = plus serré.

Avant de lancer un gros modèle, exporte la **pièce de calibration** (bouton en
haut) : une série de fentes de jeu croissant. Découpe-la, teste laquelle reçoit
ta languette avec le bon serrage, et reporte la valeur dans « Jeu d'ajustement ».

## Installation

```bash
pip install -r requirements.txt
```

(Sur Linux, il faut les libs système OpenGL/Qt habituelles :
`libgl1 libegl1 libxkbcommon0`.)

## Lancer

```bash
python app.py
```

## Utilisation en ligne de commande (sans interface)

Le moteur `slicer_core.py` est autonome et scriptable :

```python
import slicer_core as sc

mesh = sc.load_mesh("mon_modele.stl")
params = sc.SliceParams(
    method="crossed",      # ou "stacked"
    n_slices=8, n_slices_y=8,
    thickness=3.0, kerf=0.15, fit=0.05,
    target_size=150,       # redimensionne : plus grande dimension = 150 mm
    sheet_w=300, sheet_h=200,
)
slices = sc.generate_slices(mesh, params)
fichiers = sc.export_svg(slices, params, "sortie/decoupe")
sc.export_calibration_svg(params, "sortie/calibration.svg")
print(fichiers)
```

## Fichiers

- `slicer_core.py` — moteur (chargement, slicing, fentes, nesting, export SVG). Pur Python, testable en headless.
- `app.py` — interface PySide6 (barre du haut, panneau paramètres, vue 3D + vue planches).
- `test_engine.py` — vérifie le moteur et rend les SVG en PNG.
- `requirements.txt`

## Limites connues / pistes d'amélioration

- Les fentes croisées visent le milieu du plus long segment vertical : parfait
  pour des formes convexes/pleines, approximatif sur des formes très concaves
  ou creuses (une tranche peut alors nécessiter un ajustement manuel).
- Le nesting est un simple rangement « en étagères » ; un vrai nesting optimisé
  gagnerait de la matière.
- Export SVG uniquement (compatible LightBurn, Inkscape, LaserGRBL…). Le DXF
  serait un ajout facile via `ezdxf` si besoin.
- Les traits de coupe sont exportés à leur taille réelle (pas de compensation
  kerf sur les contours, seulement sur les fentes) — c'est la convention usuelle.
