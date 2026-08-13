# 🪚 LaserSlice

**Transforme un modèle 3D en pièces à découper au laser.**
Tu importes un fichier 3D (STL, OBJ, PLY, GLB…), LaserSlice le « tranche » et te
sort les **fichiers SVG** prêts à couper — avec les **encoches d'emboîtement** et
la **numérotation** pour savoir quelle pièce va où.

---

## 🚀 Démarrage en 30 secondes (Windows)

1. **Télécharge le projet** : sur la page GitHub, bouton vert **`Code`** →
   **`Download ZIP`**. Décompresse le dossier où tu veux (ton Bureau, par ex.).
2. **Double-clique sur `Lancer LaserSlice.bat`**.
3. C'est tout. 🎉

> La **première fois**, une fenêtre noire s'ouvre et installe tout ce qu'il faut
> (Python si besoin, puis les composants) — ça prend quelques minutes, c'est
> normal. Les fois **suivantes**, l'appli démarre en quelques secondes.

Si Windows affiche un avertissement « Windows a protégé votre ordinateur » :
clique sur **Informations complémentaires** → **Exécuter quand même** (c'est ton
propre fichier, il est sans danger).

---

## 🧩 À quoi ça sert concrètement

Deux façons de découper ton modèle :

- **Tranches empilées** — des couches parallèles à empiler (effet topographie /
  courbes de niveau). Une **tige d'assemblage** (trous alignés sur toutes les
  couches) est ajoutée par défaut pour que la pile tienne bien droite.
- **Squelette (colonne + côtes)** — comme les vrais puzzles bois 3D (animaux,
  avions…). L'appli repère le grand axe du corps, place une **colonne
  vertébrale** au profil du modèle, puis y plante des **côtes** perpendiculaires.
  À chaque croisement, un **joint à mi-bois** (colonne fendue par le haut, côte
  par le bas, fentes qui se rejoignent à mi-hauteur) fait tenir l'ensemble
  **sans colle**. Tu peux mettre plusieurs colonnes pour les corps larges.

**Aperçu en direct dans l'appli :**

- **Vue 3D** : ton modèle en semi-transparent (« ghost ») avec les tranches
  solides par-dessus. Si tu mets trop peu de tranches, tu vois le modèle
  « dépasser » → contrôle qualité immédiat. Un curseur **Éclaté** écarte les
  pièces pour comprendre l'assemblage.
- **Vue Planches (2D)** : les pièces mises à plat et numérotées, exactement ce
  qui sera découpé.

---

## ⚙️ Les réglages qui comptent

- **Épaisseur matière** : l'épaisseur réelle de ton contreplaqué / MDF (mesure au
  pied à coulisse — un « 3 mm » fait souvent 2.8–3.2).
- **Kerf** : la largeur du trait que ton laser brûle (~0.1–0.3 mm). La largeur
  des fentes est calculée comme `épaisseur − kerf + jeu`.
- **Jeu d'ajustement** : `+` = plus lâche, `−` = plus serré.

💡 **Astuce** : avant un gros modèle, exporte la **pièce de calibration** (bouton
en haut) — une série de fentes de jeu croissant. Découpe-la, vois laquelle reçoit
ta languette avec le bon serrage, et reporte la valeur dans « Jeu d'ajustement ».

---

## 🛠️ Installation manuelle (Mac / Linux, ou si tu préfères)

```bash
pip install -r requirements.txt
python app.py
```

Sur Linux, installe aussi les libs système habituelles :
`libgl1 libegl1 libxkbcommon0`.

---

## 🤖 Utilisation sans interface (scriptable)

Le moteur `slicer_core.py` est autonome :

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

---

## 📁 Contenu du projet

| Fichier | Rôle |
|---|---|
| **`Lancer LaserSlice.bat`** | Le lanceur double-clic (Windows) — installe et démarre tout. |
| `app.py` | L'interface (barre du haut, panneau réglages, vue 3D + vue planches). |
| `slicer_core.py` | Le moteur : chargement, slicing, fentes, nesting, export SVG. Pur Python. |
| `test_engine.py` | Vérifie le moteur et rend les SVG en PNG. |
| `requirements.txt` | La liste des composants à installer. |

---

## 📌 Limites connues / pistes d'amélioration

- Les fentes croisées visent le milieu du plus long segment vertical : parfait
  pour des formes convexes/pleines, approximatif sur des formes très concaves ou
  creuses (une tranche peut alors nécessiter un ajustement manuel).
- Le nesting est un simple rangement « en étagères » ; un vrai nesting optimisé
  gagnerait de la matière.
- Export **SVG** uniquement (compatible LightBurn, Inkscape, LaserGRBL…). Le DXF
  serait un ajout facile via `ezdxf` si besoin.
- Les traits de coupe sont exportés à leur taille réelle (compensation kerf sur
  les fentes uniquement, pas sur les contours) — c'est la convention usuelle.
