# Plugin lifecycle: toolbar button, menu entry and the dock widget.

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon

try:  # Qt6 moved QAction to QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from .qt_compat import enum


class EarthStreamPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dock = None

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.png"))
        self.action = QAction(icon, "EarthStream", self.iface.mainWindow())
        self.action.setToolTip("Stream, aggregate and analyze NASA Earthdata imagery")
        self.action.setCheckable(True)
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToRasterMenu("&EarthStream", self.action)

    def _ensure_dock(self):
        if self.dock is None:
            from .dockwidget import EarthStreamDock
            self.dock = EarthStreamDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(enum(Qt, "DockWidgetArea.RightDockWidgetArea", "RightDockWidgetArea"),
                                     self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)

    def toggle(self, checked=None):
        self._ensure_dock()
        show = not self.dock.isVisible() if checked is None else checked
        self.dock.setVisible(show)
        if show:
            self.dock.raise_()

    def unload(self):
        if self.dock is not None:
            self.dock.shutdown()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginRasterMenu("&EarthStream", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
