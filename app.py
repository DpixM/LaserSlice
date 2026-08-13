"""
app.py — Interface bureau pour le moteur de découpe (slicer_core).

Layout (d'après la maquette) :
  - Barre du haut  : Importer / Exporter SVG / Exporter calibration
  - Panneau gauche : tous les paramètres de découpe
  - Zone centrale  : onglet 3D (modèle « ghost » + tranches en direct, éclaté)
                     et onglet Planches (aperçu 2D des SVG à découper)

Dépendances : PySide6, pyqtgraph, PyOpenGL, trimesh, shapely, numpy.
Lancer :  python app.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback

import numpy as np

# --- vérification des dépendances GUI ---------------------------------------
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtSvgWidgets import QSvgWidget
    import pyqtgraph.opengl as gl
except Exception as e:  # pragma: no cover
    sys.stderr.write(
        "\nDépendances interface manquantes : " + str(e) + "\n"
        "Installez-les avec :\n"
        "    pip install PySide6 pyqtgraph PyOpenGL PyOpenGL-accelerate\n\n"
    )
    raise

import trimesh
import slicer_core as sc


# couleurs des groupes de tranches
COLOR_A = (0.95, 0.55, 0.20, 1.0)   # orange (tranches X / empilées)
COLOR_B = (0.25, 0.65, 0.95, 1.0)   # bleu (tranches Y)
COLOR_GHOST = (0.75, 0.78, 0.85, 0.22)


class SliceWorker(QtCore.QThread):
    """Calcule les tranches hors du thread GUI pour ne pas figer l'interface."""
    done = QtCore.Signal(object, object)   # (slices, prepared_mesh)
    failed = QtCore.Signal(str)

    def __init__(self, mesh, params):
        super().__init__()
        self.mesh = mesh
        self.params = params

    def run(self):
        try:
            prepared = sc.prepare_mesh(self.mesh, self.params)
            if self.params.method == "skeleton":
                slices = sc.slice_skeleton(prepared, self.params)
            else:
                slices = sc.slice_stacked(prepared, self.params)
            self.done.emit(slices, prepared)
        except Exception:
            self.failed.emit(traceback.format_exc())


class OrbitView(gl.GLViewWidget):
    """Vue 3D où le CLIC DROIT fait tourner la caméra (le clic gauche déplace,
    la molette zoome). Plus besoin de tenir le clic gauche pour pivoter."""

    def _pos(self, ev):
        return ev.position() if hasattr(ev, "position") else ev.localPos()

    def mousePressEvent(self, ev):
        self._last = self._pos(ev)
        ev.accept()

    def mouseMoveEvent(self, ev):
        pos = self._pos(ev)
        last = getattr(self, "_last", pos)
        dx, dy = pos.x() - last.x(), pos.y() - last.y()
        self._last = pos
        btns = ev.buttons()
        if btns & QtCore.Qt.RightButton:
            self.orbit(-dx, dy)                     # clic droit = pivoter
        elif btns & (QtCore.Qt.LeftButton | QtCore.Qt.MiddleButton):
            self._safe_pan(dx, dy)                  # clic gauche = déplacer
        ev.accept()

    def _safe_pan(self, dx, dy):
        for rel in ("view", True):
            try:
                self.pan(dx, dy, 0, relative=rel)
                return
            except Exception:
                continue
        try:
            self.pan(dx, dy, 0)
        except Exception:
            pass


class NumSpin(QtWidgets.QDoubleSpinBox):
    """Champ numérique qui accepte le point ET la virgule comme séparateur
    décimal (et affiche avec un point) — plus besoin de taper une virgule."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        try:
            self.setLocale(QtCore.QLocale.c())      # point décimal natif
        except Exception:
            pass
        le = self.lineEdit()
        if le is not None:
            le.textChanged.connect(self._swap_comma)

    def _swap_comma(self, text):
        # remplace la virgule par un point pendant la saisie (sans boucle infinie)
        if "," in text:
            le = self.lineEdit()
            pos = le.cursorPosition()
            le.setText(text.replace(",", "."))
            le.setCursorPosition(pos)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LaserSlice — 3D vers tranches SVG")
        self.resize(1180, 760)
        self.setAcceptDrops(True)   # glisser-déposer d'un fichier 3D

        self.mesh = None            # maillage original chargé
        self.prepared = None        # maillage préparé (centré/redimensionné)
        self.slices = []            # tranches courantes
        self.worker = None
        self._pending = False       # un recalcul a été demandé pendant un calcul

        self._build_ui()
        self._apply_theme()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- barre du haut ---
        top = QtWidgets.QFrame()
        top.setObjectName("topbar")
        top.setFixedHeight(52)
        tl = QtWidgets.QHBoxLayout(top)
        tl.setContentsMargins(12, 6, 12, 6)
        self.btn_import = QtWidgets.QPushButton("📂  Importer un modèle 3D")
        self.btn_export = QtWidgets.QPushButton("💾  Exporter les planches SVG")
        self.btn_calib = QtWidgets.QPushButton("🎯  Exporter la pièce de calibration")
        self.btn_import.setToolTip("Charge un fichier 3D (STL, OBJ, PLY, GLB…).\nAstuce : tu peux aussi glisser-déposer le fichier dans la fenêtre.")
        self.btn_export.setToolTip("Enregistre les planches SVG à découper au laser.")
        self.btn_calib.setToolTip("Exporte une pièce de test avec des fentes de jeu croissant,\npour trouver le bon serrage sur ta machine.")
        self.btn_import.clicked.connect(self.on_import)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_calib.clicked.connect(self.on_export_calibration)
        tl.addWidget(self.btn_import)
        tl.addWidget(self.btn_export)
        tl.addWidget(self.btn_calib)
        tl.addStretch(1)
        self.lbl_info = QtWidgets.QLabel("Aucun modèle — glisse un fichier 3D ici, ou clique Importer")
        tl.addWidget(self.lbl_info)
        root.addWidget(top)

        # --- corps : panneau gauche + zone centrale ---
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        body.addWidget(self._build_params_panel())
        body.addWidget(self._build_viewer(), 1)

    def _build_params_panel(self):
        panel = QtWidgets.QFrame()
        panel.setObjectName("params")
        panel.setFixedWidth(288)
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        title = QtWidgets.QLabel("Paramètres")
        title.setObjectName("panelTitle")
        lay.addWidget(title)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignLeft)
        form.setSpacing(8)

        self.cmb_method = QtWidgets.QComboBox()
        self.cmb_method.addItems(["Tranches empilées", "Squelette (colonne + côtes)"])
        self.cmb_method.currentIndexChanged.connect(self._on_method_change)
        form.addRow("Méthode", self.cmb_method)

        self.cmb_axis = QtWidgets.QComboBox()
        self.cmb_axis.addItems(["z", "y", "x"])
        form.addRow("Axe (empilé)", self.cmb_axis)

        self.spn_n = QtWidgets.QSpinBox()
        self.spn_n.setRange(1, 200); self.spn_n.setValue(12)
        form.addRow("Nb tranches / côtes", self.spn_n)

        self.spn_ny = QtWidgets.QSpinBox()
        self.spn_ny.setRange(1, 5); self.spn_ny.setValue(1)
        self.row_ny_label = QtWidgets.QLabel("Nb colonnes")
        form.addRow(self.row_ny_label, self.spn_ny)

        self.spn_thick = NumSpin()
        self.spn_thick.setRange(0.1, 50); self.spn_thick.setValue(3.0)
        self.spn_thick.setSuffix(" mm"); self.spn_thick.setSingleStep(0.1)
        form.addRow("Épaisseur matière", self.spn_thick)

        self.spn_kerf = NumSpin()
        self.spn_kerf.setRange(0.0, 2.0); self.spn_kerf.setValue(0.15)
        self.spn_kerf.setSuffix(" mm"); self.spn_kerf.setSingleStep(0.01)
        self.spn_kerf.setDecimals(2)
        form.addRow("Kerf (trait laser)", self.spn_kerf)

        self.spn_fit = NumSpin()
        self.spn_fit.setRange(-0.5, 0.5); self.spn_fit.setValue(0.05)
        self.spn_fit.setSuffix(" mm"); self.spn_fit.setSingleStep(0.01)
        self.spn_fit.setDecimals(2)
        form.addRow("Jeu d'ajustement", self.spn_fit)

        self.spn_size = NumSpin()
        self.spn_size.setRange(0, 2000); self.spn_size.setValue(0)
        self.spn_size.setSuffix(" mm"); self.spn_size.setSpecialValueText("(taille d'origine)")
        form.addRow("Redim. (+ grande dim)", self.spn_size)

        self.spn_sheet_w = NumSpin()
        self.spn_sheet_w.setRange(10, 3000); self.spn_sheet_w.setValue(300)
        self.spn_sheet_w.setSuffix(" mm")
        form.addRow("Planche largeur", self.spn_sheet_w)

        self.spn_sheet_h = NumSpin()
        self.spn_sheet_h.setRange(10, 3000); self.spn_sheet_h.setValue(200)
        self.spn_sheet_h.setSuffix(" mm")
        form.addRow("Planche hauteur", self.spn_sheet_h)

        self.chk_dowel = QtWidgets.QCheckBox("Tige d'assemblage (trous alignés)")
        self.chk_dowel.setChecked(True)   # fixations activées par défaut
        form.addRow("", self.chk_dowel)

        lay.addLayout(form)

        # --- options d'affichage ---
        sep = QtWidgets.QFrame(); sep.setFrameShape(QtWidgets.QFrame.HLine)
        lay.addWidget(sep)
        disp = QtWidgets.QLabel("Aperçu"); disp.setObjectName("panelTitle")
        lay.addWidget(disp)

        self.chk_ghost = QtWidgets.QCheckBox("Afficher le modèle (ghost)")
        self.chk_ghost.setChecked(True)
        self.chk_ghost.stateChanged.connect(self._refresh_view)
        lay.addWidget(self.chk_ghost)

        self.chk_slices = QtWidgets.QCheckBox("Afficher les tranches")
        self.chk_slices.setChecked(True)
        self.chk_slices.stateChanged.connect(self._refresh_view)
        lay.addWidget(self.chk_slices)

        exp_row = QtWidgets.QHBoxLayout()
        exp_row.addWidget(QtWidgets.QLabel("Éclaté"))
        self.sld_explode = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sld_explode.setRange(0, 600); self.sld_explode.setValue(0)
        self.sld_explode.valueChanged.connect(self._refresh_view)
        exp_row.addWidget(self.sld_explode)
        lay.addLayout(exp_row)

        lay.addStretch(1)

        self.btn_apply = QtWidgets.QPushButton("↻  Recalculer les tranches")
        self.btn_apply.setObjectName("apply")
        self.btn_apply.clicked.connect(self.recompute)
        lay.addWidget(self.btn_apply)

        self.chk_auto = QtWidgets.QCheckBox("Recalcul automatique")
        self.chk_auto.setChecked(True)
        lay.addWidget(self.chk_auto)

        # recalcul auto quand un paramètre change
        for w in (self.spn_n, self.spn_ny, self.spn_thick, self.spn_kerf,
                  self.spn_fit, self.spn_size, self.spn_sheet_w, self.spn_sheet_h):
            w.valueChanged.connect(self._auto_recompute)
        self.cmb_method.currentIndexChanged.connect(self._auto_recompute)
        self.cmb_axis.currentIndexChanged.connect(self._auto_recompute)
        self.chk_dowel.stateChanged.connect(self._auto_recompute)

        # --- bulles d'aide (au survol de la souris) ---
        self.cmb_method.setToolTip(
            "Comment découper le modèle :\n"
            "• Tranches empilées : couches horizontales à empiler (effet topographie).\n"
            "• Squelette : 1 colonne + des côtes qui s'emboîtent (comme les puzzles bois).")
        self.cmb_axis.setToolTip("Sens d'empilement des couches (méthode Tranches empilées uniquement).")
        self.spn_n.setToolTip("Nombre de tranches (empilé) ou de côtes (squelette).\nPlus élevé = plus détaillé, mais plus de pièces à assembler.")
        self.spn_ny.setToolTip("Nombre de colonnes vertébrales (squelette).\n1 pour un corps fin, 2-3 pour un corps large.")
        self.spn_thick.setToolTip("Épaisseur réelle de ta planche (contreplaqué/MDF).\nMesure-la au pied à coulisse. Elle fixe la largeur des fentes.")
        self.spn_kerf.setToolTip("Largeur du trait brûlé par le laser (~0,1 à 0,3 mm).\nSert à ajuster la largeur des fentes pour un bon serrage.")
        self.spn_fit.setToolTip("Serrage des fentes : + = plus lâche, − = plus serré.\nRègle-le grâce à la pièce de calibration.")
        self.spn_size.setToolTip("Redimensionne le modèle : sa plus grande dimension = cette valeur (mm).\n0 = garde la taille d'origine.")
        self.spn_sheet_w.setToolTip("Largeur de ta planche / matériau (pour ranger les pièces à découper).")
        self.spn_sheet_h.setToolTip("Hauteur de ta planche / matériau.")
        self.chk_dowel.setToolTip("Ajoute une tige d'assemblage : des trous alignés sur toutes\nles couches pour qu'elles tiennent bien droit (méthode empilée).")
        self.chk_ghost.setToolTip("Affiche le modèle d'origine en transparence (contrôle qualité).")
        self.chk_slices.setToolTip("Affiche ou masque les pièces découpées dans la vue 3D.")
        self.sld_explode.setToolTip("Écarte les pièces pour voir comment elles s'assemblent.")
        self.btn_apply.setToolTip("Recalcule les pièces maintenant.")
        self.chk_auto.setToolTip("Recalcule automatiquement dès qu'un réglage change.")

        self.cmb_method.setCurrentIndex(1)   # démarre directement en mode Squelette
        self._on_method_change()
        return panel

    def _build_viewer(self):
        self.tabs = QtWidgets.QTabWidget()

        # -- onglet 3D --
        self.view = OrbitView()
        self.view.setBackgroundColor((28, 32, 40))
        self.view.setCameraPosition(distance=180, elevation=18, azimuth=-60)
        grid = gl.GLGridItem(); grid.scale(10, 10, 10); grid.setDepthValue(10)
        self.view.addItem(grid)
        self.tabs.addTab(self.view, "Vue 3D")

        # -- onglet « pièces » : feuilletage façon livre --
        self.pages_root = QtWidgets.QWidget()
        self.pages_root.setObjectName("pagesRoot")
        pv = QtWidgets.QVBoxLayout(self.pages_root)
        pv.setContentsMargins(0, 0, 0, 8)
        nav = QtWidgets.QHBoxLayout()
        nav.setContentsMargins(12, 10, 12, 4)
        self.btn_prev = QtWidgets.QPushButton("‹"); self.btn_prev.setObjectName("nav")
        self.btn_next = QtWidgets.QPushButton("›"); self.btn_next.setObjectName("nav")
        self.lbl_page = QtWidgets.QLabel("—"); self.lbl_page.setObjectName("pageLabel")
        self.lbl_page.setAlignment(QtCore.Qt.AlignCenter)
        self.btn_prev.setToolTip("Pièce précédente")
        self.btn_next.setToolTip("Pièce suivante")
        self.btn_prev.clicked.connect(lambda: self._flip(-1))
        self.btn_next.clicked.connect(lambda: self._flip(1))
        nav.addStretch(1); nav.addWidget(self.btn_prev); nav.addSpacing(14)
        nav.addWidget(self.lbl_page); nav.addSpacing(14); nav.addWidget(self.btn_next)
        nav.addStretch(1)
        pv.addLayout(nav)
        self.page_view = QSvgWidget()
        self.page_view.setMinimumHeight(430)
        pv.addWidget(self.page_view, 1)
        self._page_index = 0
        self.tabs.addTab(self.pages_root, "Pièces  📖")
        self.tabs.currentChanged.connect(self._on_tab_change)

        self._gl_items = []
        return self.tabs

    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #12151b; color: #e6e6e6;
                                   font-size: 12px; }
            /* Barre du haut : fond sombre, accents jaunes (bien lisible) */
            QFrame#topbar { background:#0f1216; border-bottom:2px solid #f4d100; }
            QFrame#topbar QPushButton { background:#f4d100; color:#141414;
                border:none; padding:8px 14px; border-radius:6px; font-weight:700; }
            QFrame#topbar QPushButton:hover { background:#ffe23a; }
            QFrame#topbar QLabel { color:#f4d100; font-weight:700; font-size:13px; }
            QFrame#params { background:#0b0d11; border-right:1px solid #22262e; }
            QLabel#panelTitle { color:#f4d100; font-weight:700; font-size:13px; }
            QPushButton#apply { background:#2aa5e0; color:white; border:none;
                padding:9px; border-radius:6px; font-weight:700; }
            QPushButton#apply:hover { background:#3ab4ef; }
            QComboBox, QSpinBox, QDoubleSpinBox { background:#1a1d24;
                border:1px solid #2b303a; border-radius:4px; padding:2px 6px;
                min-height:26px; }
            /* flèches +/- : plus larges (donc bien cliquables), rendu natif conservé */
            QSpinBox::up-button, QDoubleSpinBox::up-button {
                subcontrol-position: top right; width:20px; }
            QSpinBox::down-button, QDoubleSpinBox::down-button {
                subcontrol-position: bottom right; width:20px; }
            QTabBar::tab { background:#1a1d24; padding:7px 16px; }
            QTabBar::tab:selected { background:#2aa5e0; color:white; }
            QScrollArea { background:#20242c; }
            /* Vue « livre » : fond bois sombre + boutons de feuilletage */
            QWidget#pagesRoot { background:#1a1410; }
            QLabel#pageLabel { color:#e9dcbb; font-size:14px; font-weight:700; }
            QPushButton#nav { background:#2a211a; color:#f4d100; border:1px solid #4a3c2c;
                border-radius:20px; min-width:40px; min-height:40px; font-size:18px;
                font-weight:800; }
            QPushButton#nav:hover { background:#3a2e22; }
            QPushButton#nav:disabled { color:#5a5045; border-color:#2a241c; }
        """)

    # ----------------------------------------------------------- logique
    def _params(self) -> sc.SliceParams:
        method = "skeleton" if self.cmb_method.currentIndex() == 1 else "stacked"
        size = self.spn_size.value() or None
        return sc.SliceParams(
            method=method,
            axis=self.cmb_axis.currentText(),
            n_slices=self.spn_n.value(),
            n_slices_y=self.spn_ny.value(),
            thickness=self.spn_thick.value(),
            kerf=self.spn_kerf.value(),
            fit=self.spn_fit.value(),
            target_size=size,
            sheet_w=self.spn_sheet_w.value(),
            sheet_h=self.spn_sheet_h.value(),
            dowel_holes=self.chk_dowel.isChecked(),
        )

    def _on_method_change(self):
        skeleton = self.cmb_method.currentIndex() == 1
        # "Nb colonnes" seulement pour le squelette
        self.spn_ny.setVisible(skeleton)
        self.row_ny_label.setVisible(skeleton)
        # axe d'empilement + tige : seulement pour l'empilé
        self.cmb_axis.setEnabled(not skeleton)
        self.chk_dowel.setVisible(not skeleton)

    def _auto_recompute(self, *_):
        if self.chk_auto.isChecked():
            self.recompute()

    _EXTS = (".stl", ".obj", ".ply", ".off", ".glb", ".gltf")

    def on_import(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Importer un modèle 3D", "",
            "Modèles 3D (*.stl *.obj *.ply *.off *.glb *.gltf);;Tous (*.*)")
        if path:
            self._load_path(path)

    def _load_path(self, path):
        try:
            self.mesh = sc.load_mesh(path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Erreur", f"Chargement impossible :\n{e}")
            return
        self.lbl_info.setText(os.path.basename(path))
        self.recompute()

    # --- glisser-déposer d'un fichier 3D dans la fenêtre ---
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(
                u.toLocalFile().lower().endswith(self._EXTS) for u in e.mimeData().urls()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith(self._EXTS):
                self._load_path(p)
                break

    def recompute(self):
        if self.mesh is None:
            return
        if self.worker and self.worker.isRunning():
            self._pending = True     # relancera avec les derniers paramètres à la fin
            return
        self.btn_apply.setEnabled(False)
        self.lbl_info.setText("Calcul des tranches…")
        self.worker = SliceWorker(self.mesh, self._params())
        self.worker.done.connect(self._on_slices_ready)
        self.worker.failed.connect(self._on_slices_failed)
        self.worker.start()

    def _on_slices_ready(self, slices, prepared):
        self.btn_apply.setEnabled(True)
        if self._pending:            # des paramètres ont changé pendant le calcul
            self._pending = False
            self.recompute()
            return
        self.slices = slices
        self.prepared = prepared
        na = sum(1 for s in slices if s.group == "A")
        nb = sum(1 for s in slices if s.group == "B")
        txt = f"{len(slices)} pièces" if nb else f"{len(slices)} tranches"
        if nb:
            txt += f"   ·   {na} colonne(s) + {nb} côtes"
            if nb < self.spn_n.value():
                txt += f"  (limité à {nb} : côtes trop serrées pour l'épaisseur)"
        self.lbl_info.setText(txt)
        self.btn_apply.setEnabled(True)
        self._refresh_view()
        if self.tabs.currentIndex() == 1:
            self._refresh_sheets()

    def _on_slices_failed(self, tb):
        self.btn_apply.setEnabled(True)
        if self._pending:
            self._pending = False
            self.recompute()
            return
        self.lbl_info.setText("Échec du calcul")
        QtWidgets.QMessageBox.critical(self, "Erreur de découpe", tb)

    # ----------------------------------------------------------- aperçu 3D
    def _clear_gl(self):
        for it in self._gl_items:
            self.view.removeItem(it)
        self._gl_items = []

    def _refresh_view(self, *_):
        self._clear_gl()
        # ghost
        if self.chk_ghost.isChecked() and self.prepared is not None:
            md = gl.MeshData(vertexes=self.prepared.vertices, faces=self.prepared.faces)
            ghost = gl.GLMeshItem(meshdata=md, smooth=True, color=COLOR_GHOST,
                                  glOptions="translucent", shader="shaded")
            self.view.addItem(ghost); self._gl_items.append(ghost)
        # tranches
        if self.chk_slices.isChecked() and self.slices:
            explode = self.sld_explode.value() / 100.0
            for m, group in sc.assembled_meshes(self.slices, explode=explode):
                md = gl.MeshData(vertexes=m.vertices, faces=m.faces)
                color = COLOR_A if group == "A" else COLOR_B
                item = gl.GLMeshItem(meshdata=md, smooth=False, color=color,
                                     glOptions="opaque", shader="shaded",
                                     drawEdges=True, edgeColor=(0, 0, 0, 0.4))
                self.view.addItem(item); self._gl_items.append(item)

    # ----------------------------------------------------------- aperçu 2D (livre)
    def _on_tab_change(self, idx):
        if idx == 1:
            self._refresh_sheets()

    def _refresh_sheets(self):
        n = len(self.slices)
        self._page_index = 0 if n == 0 else max(0, min(self._page_index, n - 1))
        self._show_page()

    def _flip(self, d):
        if not self.slices:
            return
        self._page_index = max(0, min(self._page_index + d, len(self.slices) - 1))
        self._show_page()

    def _show_page(self):
        n = len(self.slices)
        self.btn_prev.setEnabled(n > 0 and self._page_index > 0)
        self.btn_next.setEnabled(n > 0 and self._page_index < n - 1)
        if n == 0:
            self.lbl_page.setText("Aucune pièce — importe un modèle 3D")
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 640">'
                   '<rect width="520" height="640" fill="none"/></svg>')
        else:
            s = self.slices[self._page_index]
            self.lbl_page.setText(f"Pièce {self._page_index + 1} / {n}   ·   {s.label}")
            svg = self._slice_page_svg(s)
        self.page_view.load(QtCore.QByteArray(svg.encode("utf-8")))

    def _ring_d(self, coords, T):
        pts = [T(x, y) for x, y in coords]
        return "M" + " L".join(f"{px:.2f},{py:.2f}" for px, py in pts) + " Z"

    def _slice_page_svg(self, s):
        """Rend une tranche comme une page de livre stylée (SVG, sans filtres :
        QtSvg ne les gère pas -> ombres simulées par des formes)."""
        polys = [p for p in sc._as_polygon_list(s.polygon) if p.area > 1e-9]
        PX0, PY0, PW, PH = 92, 118, 320, 400
        if polys:
            minx = min(p.bounds[0] for p in polys)
            miny = min(p.bounds[1] for p in polys)
            maxx = max(p.bounds[2] for p in polys)
            maxy = max(p.bounds[3] for p in polys)
            pw = max(maxx - minx, 1e-6); ph = max(maxy - miny, 1e-6)
            scale = min(PW / pw, PH / ph) * 0.92
            ox = PX0 + (PW - pw * scale) / 2.0
            oy = PY0 + (PH - ph * scale) / 2.0
            T = lambda x, y: (ox + (x - minx) * scale, oy + (maxy - y) * scale)
            rings = []
            for p in polys:
                rings.append(self._ring_d(p.exterior.coords, T))
                for r in p.interiors:
                    rings.append(self._ring_d(r.coords, T))
            shape = "".join(
                f'<path d="{d}" fill="#8a6f3d" fill-opacity="0.16" '
                f'stroke="#2b2b2b" stroke-width="1.4" stroke-linejoin="round"/>'
                for d in rings)
        else:
            shape = ('<text x="252" y="330" font-size="15" fill="#8a7a55" '
                     'text-anchor="middle">(tranche vide)</text>')
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 640">
  <defs>
    <linearGradient id="paper" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#f8f0da"/><stop offset="1" stop-color="#e7d7b0"/>
    </linearGradient>
    <linearGradient id="spine" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#0000003a"/><stop offset="1" stop-color="#00000000"/>
    </linearGradient>
    <linearGradient id="curl" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#d9c79c"/><stop offset="1" stop-color="#b7a171"/>
    </linearGradient>
  </defs>
  <rect x="58" y="52" width="404" height="536" rx="10" fill="#000000" opacity="0.38"/>
  <rect x="50" y="44" width="404" height="536" rx="10" fill="url(#paper)"
        stroke="#cdb98a" stroke-width="1"/>
  <rect x="50" y="44" width="26" height="536" fill="url(#spine)"/>
  <rect x="74" y="104" width="356" height="430" rx="6" fill="none"
        stroke="#cdb98a" stroke-opacity="0.6" stroke-width="1"/>
  <text x="88" y="92" font-size="30" font-family="Georgia, serif"
        fill="#3a2f1c" font-weight="bold">{s.label}</text>
  <text x="430" y="92" font-size="12" fill="#6b5b38" text-anchor="end">LaserSlice</text>
  {shape}
  <path d="M454 580 L454 550 L424 580 Z" fill="url(#curl)"
        stroke="#a88f5f" stroke-width="0.8"/>
  <text x="252" y="566" font-size="12" fill="#6b5b38"
        text-anchor="middle">à découper — trait noir = coupe</text>
</svg>'''

    # ----------------------------------------------------------- exports
    def on_export(self):
        if not self.slices:
            QtWidgets.QMessageBox.information(self, "Info", "Chargez d'abord un modèle.")
            return
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Dossier d'export")
        if not d:
            return
        prefix = os.path.join(d, "decoupe")
        files = sc.export_svg(self.slices, self._params(), prefix)
        QtWidgets.QMessageBox.information(
            self, "Export terminé",
            f"{len(files)} planche(s) exportée(s) dans :\n{d}")

    def on_export_calibration(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Exporter la calibration", "calibration.svg", "SVG (*.svg)")
        if not path:
            return
        sc.export_calibration_svg(self._params(), path)
        QtWidgets.QMessageBox.information(self, "OK", f"Gabarit enregistré :\n{path}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Écrit l'erreur dans un fichier à côté de l'appli pour pouvoir la diagnostiquer.
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "laserslice_erreur.txt")
        try:
            with open(log, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        sys.stderr.write("\n" + traceback.format_exc() + "\n")
        raise
