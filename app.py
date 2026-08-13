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
            if self.params.method == "crossed":
                slices = sc.slice_crossed(prepared, self.params)
            else:
                slices = sc.slice_stacked(prepared, self.params)
            self.done.emit(slices, prepared)
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LaserSlice — 3D vers tranches SVG")
        self.resize(1180, 760)

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
        self.btn_import.clicked.connect(self.on_import)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_calib.clicked.connect(self.on_export_calibration)
        tl.addWidget(self.btn_import)
        tl.addWidget(self.btn_export)
        tl.addWidget(self.btn_calib)
        tl.addStretch(1)
        self.lbl_info = QtWidgets.QLabel("Aucun modèle chargé")
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
        self.cmb_method.addItems(["Tranches empilées", "Emboîtement en croix"])
        self.cmb_method.currentIndexChanged.connect(self._on_method_change)
        form.addRow("Méthode", self.cmb_method)

        self.cmb_axis = QtWidgets.QComboBox()
        self.cmb_axis.addItems(["z", "y", "x"])
        form.addRow("Axe (empilé)", self.cmb_axis)

        self.spn_n = QtWidgets.QSpinBox()
        self.spn_n.setRange(1, 200); self.spn_n.setValue(12)
        form.addRow("Nb tranches", self.spn_n)

        self.spn_ny = QtWidgets.QSpinBox()
        self.spn_ny.setRange(1, 200); self.spn_ny.setValue(8)
        self.row_ny_label = QtWidgets.QLabel("Nb tranches (2e axe)")
        form.addRow(self.row_ny_label, self.spn_ny)

        self.spn_thick = QtWidgets.QDoubleSpinBox()
        self.spn_thick.setRange(0.1, 50); self.spn_thick.setValue(3.0)
        self.spn_thick.setSuffix(" mm"); self.spn_thick.setSingleStep(0.1)
        form.addRow("Épaisseur matière", self.spn_thick)

        self.spn_kerf = QtWidgets.QDoubleSpinBox()
        self.spn_kerf.setRange(0.0, 2.0); self.spn_kerf.setValue(0.15)
        self.spn_kerf.setSuffix(" mm"); self.spn_kerf.setSingleStep(0.01)
        self.spn_kerf.setDecimals(2)
        form.addRow("Kerf (trait laser)", self.spn_kerf)

        self.spn_fit = QtWidgets.QDoubleSpinBox()
        self.spn_fit.setRange(-0.5, 0.5); self.spn_fit.setValue(0.05)
        self.spn_fit.setSuffix(" mm"); self.spn_fit.setSingleStep(0.01)
        self.spn_fit.setDecimals(2)
        form.addRow("Jeu d'ajustement", self.spn_fit)

        self.spn_size = QtWidgets.QDoubleSpinBox()
        self.spn_size.setRange(0, 2000); self.spn_size.setValue(0)
        self.spn_size.setSuffix(" mm"); self.spn_size.setSpecialValueText("(taille d'origine)")
        form.addRow("Redim. (+ grande dim)", self.spn_size)

        self.spn_sheet_w = QtWidgets.QDoubleSpinBox()
        self.spn_sheet_w.setRange(10, 3000); self.spn_sheet_w.setValue(300)
        self.spn_sheet_w.setSuffix(" mm")
        form.addRow("Planche largeur", self.spn_sheet_w)

        self.spn_sheet_h = QtWidgets.QDoubleSpinBox()
        self.spn_sheet_h.setRange(10, 3000); self.spn_sheet_h.setValue(200)
        self.spn_sheet_h.setSuffix(" mm")
        form.addRow("Planche hauteur", self.spn_sheet_h)

        self.chk_dowel = QtWidgets.QCheckBox("Trous d'alignement (tige)")
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
        self.sld_explode.setRange(0, 200); self.sld_explode.setValue(0)
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

        self._on_method_change()
        return panel

    def _build_viewer(self):
        self.tabs = QtWidgets.QTabWidget()

        # -- onglet 3D --
        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor((28, 32, 40))
        self.view.setCameraPosition(distance=180, elevation=18, azimuth=-60)
        grid = gl.GLGridItem(); grid.scale(10, 10, 10); grid.setDepthValue(10)
        self.view.addItem(grid)
        self.tabs.addTab(self.view, "Vue 3D")

        # -- onglet planches 2D --
        self.sheets_area = QtWidgets.QScrollArea()
        self.sheets_area.setWidgetResizable(True)
        self.sheets_host = QtWidgets.QWidget()
        self.sheets_layout = QtWidgets.QVBoxLayout(self.sheets_host)
        self.sheets_area.setWidget(self.sheets_host)
        self.tabs.addTab(self.sheets_area, "Planches (2D)")
        self.tabs.currentChanged.connect(self._on_tab_change)

        self._gl_items = []
        return self.tabs

    def _apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #12151b; color: #e6e6e6;
                                   font-size: 12px; }
            QFrame#topbar { background: #f4d100; }
            QFrame#topbar QPushButton { background:#1c1f26; color:#f4d100;
                border:none; padding:8px 12px; border-radius:6px; font-weight:600; }
            QFrame#topbar QPushButton:hover { background:#2a2e38; }
            QFrame#topbar QLabel { color:#1c1f26; font-weight:600; }
            QFrame#params { background:#0b0d11; border-right:1px solid #22262e; }
            QLabel#panelTitle { color:#f4d100; font-weight:700; font-size:13px; }
            QPushButton#apply { background:#2aa5e0; color:white; border:none;
                padding:9px; border-radius:6px; font-weight:700; }
            QPushButton#apply:hover { background:#3ab4ef; }
            QComboBox, QSpinBox, QDoubleSpinBox { background:#1a1d24;
                border:1px solid #2b303a; border-radius:4px; padding:3px; }
            QTabBar::tab { background:#1a1d24; padding:7px 16px; }
            QTabBar::tab:selected { background:#2aa5e0; color:white; }
            QScrollArea { background:#20242c; }
        """)

    # ----------------------------------------------------------- logique
    def _params(self) -> sc.SliceParams:
        method = "crossed" if self.cmb_method.currentIndex() == 1 else "stacked"
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
        crossed = self.cmb_method.currentIndex() == 1
        self.spn_ny.setVisible(crossed)
        self.row_ny_label.setVisible(crossed)
        self.cmb_axis.setEnabled(not crossed)

    def _auto_recompute(self, *_):
        if self.chk_auto.isChecked():
            self.recompute()

    def on_import(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Importer un modèle 3D", "",
            "Modèles 3D (*.stl *.obj *.ply *.off *.glb *.gltf);;Tous (*.*)")
        if not path:
            return
        try:
            self.mesh = sc.load_mesh(path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Erreur", f"Chargement impossible :\n{e}")
            return
        self.lbl_info.setText(os.path.basename(path))
        self.recompute()

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
        txt = f"{len(slices)} tranches"
        if nb:
            txt += f" (A:{na} / B:{nb})"
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

    # ----------------------------------------------------------- aperçu 2D
    def _on_tab_change(self, idx):
        if idx == 1:
            self._refresh_sheets()

    def _refresh_sheets(self):
        # vide
        while self.sheets_layout.count():
            it = self.sheets_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self.slices:
            self.sheets_layout.addWidget(QtWidgets.QLabel("Aucune tranche à afficher."))
            return
        tmp = tempfile.mkdtemp(prefix="laserslice_")
        files = sc.export_svg(self.slices, self._params(), os.path.join(tmp, "planche"))
        for f in files:
            lbl = QtWidgets.QLabel(os.path.basename(f)); lbl.setStyleSheet("color:#9fb;")
            self.sheets_layout.addWidget(lbl)
            w = QSvgWidget(f)
            w.setFixedHeight(360)
            self.sheets_layout.addWidget(w)
        self.sheets_layout.addStretch(1)

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
    main()
