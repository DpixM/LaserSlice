import os, sys, time
import trimesh
from PySide6 import QtWidgets, QtCore
import app as A

os.makedirs("out", exist_ok=True)

qt = QtWidgets.QApplication(sys.argv)
win = A.MainWindow()
win.resize(1180, 760)
win.show()

# modèle de test
mesh = trimesh.util.concatenate([
    trimesh.creation.icosphere(subdivisions=3, radius=38),
    trimesh.creation.cylinder(radius=14, height=88),
])
win.mesh = mesh

def pump(ms):
    end = time.time() + ms/1000.0
    while time.time() < end:
        qt.processEvents(QtCore.QEventLoop.AllEvents, 20)
        time.sleep(0.01)

# --- test 1 : méthode croisée ---
win.cmb_method.setCurrentIndex(1)   # emboîtement en croix
win.spn_n.setValue(6); win.spn_ny.setValue(6)
win.recompute()
# attendre le worker
for _ in range(300):
    pump(50)
    if win.slices and not (win.worker and win.worker.isRunning()):
        break
print("crossed slices:", len(win.slices))
win.sld_explode.setValue(60)
win._refresh_view()
pump(300)
img = win.view.grabFramebuffer()
img.save("out/gui_3d_crossed_explode.png")

win.sld_explode.setValue(0)
win._refresh_view()
pump(300)
win.view.grabFramebuffer().save("out/gui_3d_crossed.png")

# onglet planches
win.tabs.setCurrentIndex(1)
pump(500)
win.grab().save("out/gui_sheets.png")

# --- test 2 : méthode empilée ---
win.tabs.setCurrentIndex(0)
win.cmb_method.setCurrentIndex(0)
win.spn_n.setValue(14)
win.recompute()
for _ in range(300):
    pump(50)
    if win.slices and not (win.worker and win.worker.isRunning()):
        break
print("stacked slices:", len(win.slices))
win.sld_explode.setValue(40)
win._refresh_view()
pump(300)
win.view.grabFramebuffer().save("out/gui_3d_stacked.png")

# capture fenêtre complète
win.sld_explode.setValue(0)
win._refresh_view()
pump(300)
win.grab().save("out/gui_full.png")

# export SVG headless
files = A.sc.export_svg(win.slices, win._params(), "out/gui_export")
print("exported:", len(files), "planche(s)")
print("SMOKE OK")
qt.quit()
