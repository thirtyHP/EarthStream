# The EarthStream panel. Same flow as SatStream: pick a platform, sign in, set the
# filters, search the map extent, work from the Imagery Footprint table. The
# Aggregate and Analyze section is new here and runs the processing engine on the
# selected scenes.

import datetime
import json
import os

from qgis.PyQt.QtCore import QDate, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsVectorLayer,
    QgsWkbTypes,
)

from . import credentials, layers, tasks
from .providers import AGGREGATIONS, EDL_PROFILE_URL, PROVIDERS, products_for
from .qt_compat import enum, msg_level

S = "EarthStream/"
MAX_LOAD = 12

USER_ROLE = enum(Qt, "ItemDataRole.UserRole", "UserRole")
DISPLAY_ROLE = enum(Qt, "ItemDataRole.DisplayRole", "DisplayRole")


def _is_polygon_layer(layer):
    if not isinstance(layer, QgsVectorLayer):
        return False
    try:
        return layer.geometryType() == Qgis.GeometryType.Polygon
    except AttributeError:
        return layer.geometryType() == QgsWkbTypes.PolygonGeometry


class EarthStreamDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("EarthStream", parent)
        self.setObjectName("EarthStreamDock")
        self.iface = iface
        self.settings = QgsSettings()
        self.cache_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "earthstream")
        os.makedirs(self.cache_dir, exist_ok=True)

        self.scenes = []
        self.row_fid = {}
        self.footprint_id = None
        self.layer_ids = []
        self.task = None

        self._build_ui()
        self._restore()
        self._restore_session()
        self.on_platform_changed()

        proj = QgsProject.instance()
        proj.layersAdded.connect(self.refresh_aoi_layers)
        proj.layersRemoved.connect(self.refresh_aoi_layers)
        self.refresh_aoi_layers()

    # ===== UI ===========================================================================

    def _build_ui(self):
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(8)

        # Platform Selection
        box = QGroupBox("Platform Selection")
        lay = QVBoxLayout(box)
        self.platform = QComboBox()
        for name, cfg in PROVIDERS.items():
            self.platform.addItem("%s  [%s]" % (name, cfg["gsd_label"]), name)
        self.platform.currentIndexChanged.connect(self.on_platform_changed)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: palette(mid); font-size: 11px;")
        lay.addWidget(self.platform)
        lay.addWidget(self.note)
        col.addWidget(box)

        # Credentials
        box = QGroupBox("Earthdata Login")
        form = QFormLayout(box)
        self.user = QLineEdit()
        self.user.setPlaceholderText("Earthdata username")
        self.password = QLineEdit()
        self.password.setEchoMode(enum(QLineEdit, "EchoMode.Password", "Password"))
        self.password.setPlaceholderText("Password (never stored)")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(enum(QLineEdit, "EchoMode.Password", "Password"))
        self.token_edit.setPlaceholderText("or paste a user token instead")
        self.remember = QCheckBox("Remember (keeps username and token in QGIS settings)")
        row = QHBoxLayout()
        self.btn_signin = QPushButton("Sign In")
        self.btn_signin.clicked.connect(self.sign_in)
        self.btn_signout = QPushButton("Sign Out")
        self.btn_signout.clicked.connect(self.sign_out)
        row.addWidget(self.btn_signin)
        row.addWidget(self.btn_signout)
        self.auth_status = QLabel()
        self.auth_status.setWordWrap(True)
        link = QLabel('<a href="%s">Create a free account or generate a token</a>' % EDL_PROFILE_URL)
        link.setOpenExternalLinks(True)
        form.addRow("Username", self.user)
        form.addRow("Password", self.password)
        form.addRow("Token", self.token_edit)
        form.addRow(self.remember)
        form.addRow(row)
        form.addRow(self.auth_status)
        form.addRow(link)
        col.addWidget(box)

        # Filters
        box = QGroupBox("Filters")
        grid = QGridLayout(box)
        self.cloud_slider = QSlider(enum(Qt, "Orientation.Horizontal", "Horizontal"))
        self.cloud_slider.setRange(0, 100)
        self.cloud_spin = QSpinBox()
        self.cloud_spin.setRange(0, 100)
        self.cloud_spin.setSuffix(" %")
        self.cloud_slider.valueChanged.connect(self.cloud_spin.setValue)
        self.cloud_spin.valueChanged.connect(self.cloud_slider.setValue)
        self.start = QDateEdit()
        self.end = QDateEdit()
        for d in (self.start, self.end):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        self.limit = QSpinBox()
        self.limit.setRange(10, 2000)
        self.limit.setSingleStep(50)
        self.aoi = QComboBox()
        self.aoi.setToolTip("Search, clip and analyze within the current map view, or within a polygon layer "
                            "(its selected features if any are selected).")
        self.btn_search = QPushButton("Search")
        self.btn_search.clicked.connect(self.search)
        grid.addWidget(QLabel("Max cloud"), 0, 0)
        grid.addWidget(self.cloud_slider, 0, 1)
        grid.addWidget(self.cloud_spin, 0, 2)
        grid.addWidget(QLabel("Start"), 1, 0)
        grid.addWidget(self.start, 1, 1, 1, 2)
        grid.addWidget(QLabel("End"), 2, 0)
        grid.addWidget(self.end, 2, 1, 1, 2)
        grid.addWidget(QLabel("Max results"), 3, 0)
        grid.addWidget(self.limit, 3, 1, 1, 2)
        grid.addWidget(QLabel("Area"), 4, 0)
        grid.addWidget(self.aoi, 4, 1, 1, 2)
        grid.addWidget(self.btn_search, 5, 0, 1, 3)
        col.addWidget(box)

        # Imagery Footprint
        box = QGroupBox("Imagery Footprint")
        lay = QVBoxLayout(box)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Granule", "Date (UTC)", "Cloud %", "Tile", "Sensor"])
        self.table.setSelectionBehavior(enum(QAbstractItemView, "SelectionBehavior.SelectRows", "SelectRows"))
        self.table.setSelectionMode(enum(QAbstractItemView, "SelectionMode.ExtendedSelection",
                                         "ExtendedSelection"))
        self.table.setEditTriggers(enum(QAbstractItemView, "EditTrigger.NoEditTriggers", "NoEditTriggers"))
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(enum(QHeaderView, "ResizeMode.Interactive", "Interactive"))
        hdr.setStretchLastSection(True)
        self.table.setColumnWidth(0, 230)
        self.table.setMinimumHeight(180)
        self.table.itemSelectionChanged.connect(self.on_selection_changed)
        self.table.doubleClicked.connect(lambda _i: self.load("visual"))
        self.count_label = QLabel("No search yet.")
        row = QHBoxLayout()
        b_all = QPushButton("Select All")
        b_all.clicked.connect(self.table.selectAll)
        b_zoom = QPushButton("Zoom to Selected")
        b_zoom.clicked.connect(self.zoom_selected)
        row.addWidget(b_all)
        row.addWidget(b_zoom)
        lay.addWidget(self.table)
        lay.addWidget(self.count_label)
        lay.addLayout(row)
        col.addWidget(box)

        # Tools
        box = QGroupBox("Tools")
        grid = QGridLayout(box)
        self.btn_visual = QPushButton("Load Visual")
        self.btn_visual.clicked.connect(lambda: self.load("visual"))
        self.btn_ms = QPushButton("Load Multispectral")
        self.btn_ms.clicked.connect(lambda: self.load("ms"))
        self.btn_pan = QPushButton("Load Panchromatic")
        self.btn_pan.clicked.connect(self.load_pan)
        self.btn_download = QPushButton("Download Selected")
        self.btn_download.clicked.connect(self.download)
        self.clip = QCheckBox("Clip to search area")
        self.clip.setToolTip("Reads only the part of each file inside the area (much smaller downloads).")
        self.add_downloads = QCheckBox("Add downloads to map")
        self.out_dir = QLineEdit()
        b_dir = QPushButton("...")
        b_dir.setMaximumWidth(32)
        b_dir.clicked.connect(self.pick_out_dir)
        b_open = QPushButton("Open Folder")
        b_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir.text())))
        self.btn_clear = QPushButton("Clear Layers")
        self.btn_clear.clicked.connect(self.clear_layers)
        grid.addWidget(self.btn_visual, 0, 0)
        grid.addWidget(self.btn_ms, 0, 1)
        grid.addWidget(self.btn_pan, 1, 0)
        grid.addWidget(self.btn_clear, 1, 1)
        grid.addWidget(self.btn_download, 2, 0, 1, 2)
        grid.addWidget(self.clip, 3, 0)
        grid.addWidget(self.add_downloads, 3, 1)
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output"))
        out_row.addWidget(self.out_dir)
        out_row.addWidget(b_dir)
        grid.addLayout(out_row, 4, 0, 1, 2)
        grid.addWidget(b_open, 5, 0, 1, 2)
        col.addWidget(box)

        # Aggregate and Analyze
        box = QGroupBox("Aggregate and Analyze")
        form = QFormLayout(box)
        self.product = QComboBox()
        self.method = QComboBox()
        for name, tip in AGGREGATIONS:
            self.method.addItem(name)
            self.method.setItemData(self.method.count() - 1, tip, enum(Qt, "ItemDataRole.ToolTipRole",
                                                                        "ToolTipRole"))
        self.res = QDoubleSpinBox()
        self.res.setRange(10, 5000)
        self.res.setDecimals(0)
        self.res.setSuffix(" m")
        self.mask_clouds = QCheckBox("Clouds and shadows")
        self.mask_snow = QCheckBox("Snow and ice")
        self.mask_water = QCheckBox("Water")
        masks = QHBoxLayout()
        masks.addWidget(self.mask_clouds)
        masks.addWidget(self.mask_snow)
        masks.addWidget(self.mask_water)
        self.want_composite = QCheckBox("Composite raster")
        self.want_series = QCheckBox("Time-series table and chart")
        outs = QHBoxLayout()
        outs.addWidget(self.want_composite)
        outs.addWidget(self.want_series)
        self.btn_analyze = QPushButton("Run Analysis on Selected")
        self.btn_analyze.clicked.connect(self.analyze)
        form.addRow("Product", self.product)
        form.addRow("Aggregate", self.method)
        form.addRow("Resolution", self.res)
        form.addRow("Mask", masks)
        form.addRow("Outputs", outs)
        form.addRow(self.btn_analyze)
        col.addWidget(box)

        col.addStretch(1)

        # status strip
        footer = QWidget()
        f = QVBoxLayout(footer)
        f.setContentsMargins(6, 0, 6, 6)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        srow = QHBoxLayout()
        self.status = QLabel("Ready.")
        self.status.setWordWrap(True)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        srow.addWidget(self.status, 1)
        srow.addWidget(self.btn_cancel)
        f.addWidget(self.progress)
        f.addLayout(srow)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(0)
        ol.addWidget(scroll, 1)
        ol.addWidget(footer)
        self.setWidget(outer)

        self.action_buttons = [self.btn_search, self.btn_visual, self.btn_ms, self.btn_pan, self.btn_download,
                               self.btn_analyze, self.btn_signin]

    # ===== settings =====================================================================

    def _restore(self):
        s = self.settings
        idx = self.platform.findData(s.value(S + "platform", ""))
        if idx >= 0:
            self.platform.setCurrentIndex(idx)
        self.cloud_spin.setValue(int(s.value(S + "cloud", 30)))
        today = QDate.currentDate()
        self.end.setDate(today)
        self.start.setDate(today.addDays(-int(s.value(S + "days_back", 60))))
        self.limit.setValue(int(s.value(S + "limit", 200)))
        self.out_dir.setText(s.value(S + "out_dir", os.path.join(os.path.expanduser("~"), "EarthStream")))
        self.clip.setChecked(s.value(S + "clip", "true") == "true")
        self.add_downloads.setChecked(s.value(S + "add_downloads", "true") == "true")
        self.mask_clouds.setChecked(s.value(S + "mask_clouds", "true") == "true")
        self.mask_snow.setChecked(s.value(S + "mask_snow", "false") == "true")
        self.mask_water.setChecked(s.value(S + "mask_water", "false") == "true")
        self.want_composite.setChecked(s.value(S + "want_composite", "true") == "true")
        self.want_series.setChecked(s.value(S + "want_series", "true") == "true")
        midx = self.method.findText(s.value(S + "method", "Median"))
        self.method.setCurrentIndex(max(0, midx))
        self.remember.setChecked(s.value(S + "remember", "false") == "true")
        self.user.setText(s.value(S + "username", ""))

    def save_settings(self):
        s = self.settings
        b = lambda w: "true" if w.isChecked() else "false"  # noqa: E731
        s.setValue(S + "platform", self.platform.currentData())
        s.setValue(S + "cloud", self.cloud_spin.value())
        s.setValue(S + "days_back", max(1, self.start.date().daysTo(self.end.date())))
        s.setValue(S + "limit", self.limit.value())
        s.setValue(S + "out_dir", self.out_dir.text())
        s.setValue(S + "clip", b(self.clip))
        s.setValue(S + "add_downloads", b(self.add_downloads))
        s.setValue(S + "mask_clouds", b(self.mask_clouds))
        s.setValue(S + "mask_snow", b(self.mask_snow))
        s.setValue(S + "mask_water", b(self.mask_water))
        s.setValue(S + "want_composite", b(self.want_composite))
        s.setValue(S + "want_series", b(self.want_series))
        s.setValue(S + "method", self.method.currentText())
        s.setValue(S + "remember", b(self.remember))

    def _restore_session(self):
        tok = self.settings.value(S + "token", "")
        exp = self.settings.value(S + "token_expires", "")
        if tok and self.remember.isChecked():
            exp_date = None
            try:
                exp_date = datetime.date.fromisoformat(exp) if exp else None
            except ValueError:
                pass
            credentials.set_session(tok, self.user.text() or None, exp_date, self.cache_dir)
            if not credentials.is_signed_in():
                credentials.clear_session(self.cache_dir)
        else:
            credentials.configure_gdal(None, self.cache_dir)
        self._update_auth_label()

    # ===== platform ======================================================================

    def cfg(self):
        return PROVIDERS[self.platform.currentData()]

    def on_platform_changed(self, *_):
        cfg = self.cfg()
        self.note.setText(cfg["note"])
        self.cloud_slider.setEnabled(cfg["supports_cloud"])
        self.cloud_spin.setEnabled(cfg["supports_cloud"])
        dem = cfg["kind"] == "dem"
        self.start.setEnabled(not dem)
        self.end.setEnabled(not dem)
        self.btn_visual.setEnabled(cfg["streamable"])
        self.btn_ms.setEnabled(bool(cfg.get("ms_bands")))
        self.clip.setEnabled(cfg["streamable"])

        self.product.clear()
        for name, spec in products_for(self.platform.currentData()).items():
            self.product.addItem(name)
            self.product.setItemData(self.product.count() - 1, spec.get("tip", ""),
                                     enum(Qt, "ItemDataRole.ToolTipRole", "ToolTipRole"))
        analyzable = cfg["streamable"] and self.product.count() > 0
        self.btn_analyze.setEnabled(analyzable)
        self.product.setEnabled(analyzable)
        self.method.setEnabled(analyzable and not dem)
        self.res.setValue(cfg["native_res"])
        opt = cfg["kind"] == "reflectance"
        self.mask_clouds.setEnabled(opt or cfg["kind"] == "lst")
        self.mask_snow.setEnabled(opt)
        self.mask_water.setEnabled(opt)
        self._clear_results()

    # ===== Earthdata Login ===============================================================

    def _update_auth_label(self):
        if credentials.is_signed_in():
            who = credentials.username() or "token"
            exp = credentials.expires()
            text = "Signed in as %s" % who
            if exp:
                text += " (token valid until %s)" % exp.isoformat()
            self.auth_status.setText('<span style="color:#2e7d32;">%s</span>' % text)
        else:
            self.auth_status.setText('<span style="color:#b71c1c;">Not signed in. Searching works without '
                                     'an account; streaming and downloads need one.</span>')

    def sign_in(self):
        tok = self.token_edit.text().strip()
        user = self.user.text().strip()
        if tok:
            credentials.set_session(tok, user or None, None, self.cache_dir)
            self.token_edit.clear()
            self._after_sign_in()
            return
        pwd = self.password.text()
        if not user or not pwd:
            self.warn("Enter your Earthdata username and password, or paste a token.")
            return
        self.run_task("Earthdata sign in", tasks.signin_work(user, pwd), self._signed_in)

    def _signed_in(self, result):
        tok, exp = result
        credentials.set_session(tok, self.user.text().strip(), exp, self.cache_dir)
        self.password.clear()
        self._after_sign_in()

    def _after_sign_in(self):
        if self.remember.isChecked():
            self.settings.setValue(S + "username", self.user.text().strip())
            self.settings.setValue(S + "token", credentials.token())
            exp = credentials.expires()
            self.settings.setValue(S + "token_expires", exp.isoformat() if exp else "")
        self.settings.setValue(S + "remember", "true" if self.remember.isChecked() else "false")
        self._update_auth_label()
        self.set_status("Signed in. Protected imagery will stream into QGIS.")

    def sign_out(self):
        credentials.clear_session(self.cache_dir)
        for key in ("token", "token_expires"):
            self.settings.remove(S + key)
        self._update_auth_label()
        self.set_status("Signed out.")

    def require_sign_in(self):
        if credentials.is_signed_in():
            return True
        self.warn("Sign in with Earthdata Login first. NASA's cloud archives need an account to stream "
                  "or download data (it's free).")
        return False

    # ===== area of interest ===============================================================

    def refresh_aoi_layers(self, *_):
        current = self.aoi.currentData()
        self.aoi.blockSignals(True)
        self.aoi.clear()
        self.aoi.addItem("Current map extent", None)
        for lyr in QgsProject.instance().mapLayers().values():
            if _is_polygon_layer(lyr) and lyr.id() != self.footprint_id:
                self.aoi.addItem(lyr.name(), lyr.id())
        idx = self.aoi.findData(current)
        self.aoi.setCurrentIndex(idx if idx >= 0 else 0)
        self.aoi.blockSignals(False)

    def get_aoi(self):
        """Return (bbox in lon/lat, GeoJSON path or None)."""
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        lid = self.aoi.currentData()
        if not lid:
            canvas = self.iface.mapCanvas()
            xf = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), wgs, QgsProject.instance())
            r = xf.transformBoundingBox(canvas.extent())
            return self._clamp(r), None
        layer = QgsProject.instance().mapLayer(lid)
        if layer is None:
            raise RuntimeError("The area layer is no longer in the project.")
        feats = layer.selectedFeatures() or list(layer.getFeatures())
        if not feats:
            raise RuntimeError("%s has no features." % layer.name())
        if len(feats) > 5000:
            raise RuntimeError("%s has %d features; select the ones to use (5000 at most)."
                               % (layer.name(), len(feats)))
        xf = QgsCoordinateTransform(layer.crs(), wgs, QgsProject.instance())
        bbox = QgsRectangle()
        bbox.setMinimal()
        out = []
        for ft in feats:
            g = ft.geometry()
            if g is None or g.isEmpty():
                continue
            g.transform(xf)
            bbox.combineExtentWith(g.boundingBox())
            out.append({"type": "Feature", "properties": {}, "geometry": json.loads(g.asJson())})
        path = os.path.join(self.cache_dir, "aoi.geojson")
        with open(path, "w") as fh:
            json.dump({"type": "FeatureCollection", "features": out}, fh)
        return self._clamp(bbox), path

    @staticmethod
    def _clamp(r):
        return (max(-180.0, r.xMinimum()), max(-90.0, r.yMinimum()),
                min(180.0, r.xMaximum()), min(90.0, r.yMaximum()))

    # ===== search ==========================================================================

    def search(self):
        try:
            bbox, _ = self.get_aoi()
        except Exception as e:
            self.warn(str(e))
            return
        if self.start.date() > self.end.date():
            self.warn("The start date is after the end date.")
            return
        self.save_settings()
        cfg = self.cfg()
        cloud = self.cloud_spin.value() if cfg["supports_cloud"] else None
        work = tasks.search_work(self.platform.currentData(), bbox,
                                 self.start.date().toString("yyyy-MM-dd"),
                                 self.end.date().toString("yyyy-MM-dd"), cloud, self.limit.value())
        self.run_task("EarthStream search", work, self._search_done)

    def _search_done(self, result):
        scenes, hits = result
        self._clear_results()
        self.scenes = scenes
        credentials.register_hosts(u for sc in scenes for u in sc["links"])
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(scenes))
        for row, sc in enumerate(scenes):
            items = [
                QTableWidgetItem(sc["id"]),
                QTableWidgetItem(sc["datetime"][:16].replace("T", " ")),
                QTableWidgetItem(),
                QTableWidgetItem(sc["tile"]),
                QTableWidgetItem(sc["sensor"]),
            ]
            items[0].setData(USER_ROLE, row)
            items[0].setToolTip(sc["id"])
            if sc["cloud"] is not None:
                items[2].setData(DISPLAY_ROLE, round(sc["cloud"], 1))
            for c, it in enumerate(items):
                self.table.setItem(row, c, it)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, enum(Qt, "SortOrder.DescendingOrder", "DescendingOrder"))
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(0, min(self.table.columnWidth(0), 170))

        if scenes:
            fp = layers.footprint_layer(scenes)
            self.footprint_id = fp.id()
            self.row_fid = {f["row"]: f.id() for f in fp.getFeatures()}
            self.refresh_aoi_layers()
        more = " of %d" % hits if hits > len(scenes) else ""
        self.count_label.setText("%d scene(s)%s. Ctrl or Shift click to select several." % (len(scenes), more))
        self.set_status("Found %d scene(s)%s." % (len(scenes), more) if scenes else
                        "No scenes matched. Try a wider date range, a higher cloud limit or a larger area.")

    def _clear_results(self):
        self.scenes = []
        self.row_fid = {}
        self.table.setRowCount(0)
        self.count_label.setText("No search yet.")
        if self.footprint_id:
            if QgsProject.instance().mapLayer(self.footprint_id):
                QgsProject.instance().removeMapLayer(self.footprint_id)
            self.footprint_id = None

    def selected_scenes(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        out = []
        for r in rows:
            item = self.table.item(r, 0)
            if item is not None:
                out.append(self.scenes[int(item.data(USER_ROLE))])
        return out

    def _selected_indexes(self):
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        return [int(self.table.item(r, 0).data(USER_ROLE)) for r in rows if self.table.item(r, 0)]

    def on_selection_changed(self):
        fp = QgsProject.instance().mapLayer(self.footprint_id) if self.footprint_id else None
        if fp is None:
            return
        fp.selectByIds([self.row_fid[i] for i in self._selected_indexes() if i in self.row_fid])

    def zoom_selected(self):
        fp = QgsProject.instance().mapLayer(self.footprint_id) if self.footprint_id else None
        if fp is None or not fp.selectedFeatureCount():
            self.warn("Select one or more scenes in the table first.")
            return
        self.iface.mapCanvas().zoomToSelected(fp)
        self.iface.mapCanvas().refresh()

    # ===== tools =============================================================================

    def _need_selection(self, limit=None):
        scenes = self.selected_scenes()
        if not scenes:
            self.warn("Select one or more scenes in the Imagery Footprint table.")
            return None
        if limit and len(scenes) > limit:
            self.warn("%d scenes are selected. Load at most %d at a time to keep QGIS responsive."
                      % (len(scenes), limit))
            return None
        return scenes

    def load(self, mode):
        cfg = self.cfg()
        if not cfg["streamable"]:
            self.warn(cfg.get("stream_message", "This platform can't be streamed."))
            return
        if mode == "ms" and not cfg.get("ms_bands"):
            self.info(cfg.get("ms_message", "This platform has no multispectral bands."))
            return
        scenes = self._need_selection(MAX_LOAD)
        if not scenes or not self.require_sign_in():
            return
        work = tasks.load_work(self.platform.currentData(), scenes, mode, os.path.join(self.cache_dir, "vrt"))
        self.run_task("EarthStream load", work, self._add_specs)

    def load_pan(self):
        self.info(self.cfg().get("pan_message", "No panchromatic band for this platform."))

    def _add_specs(self, result):
        added = 0
        for spec in result["specs"]:
            layer, err = layers.add_raster(spec)
            if layer:
                self.layer_ids.append(layer.id())
                added += 1
            elif err:
                result["problems"].append(err)
        self.set_status("Added %d layer(s)." % added)
        if result.get("problems"):
            self.report_problems(result["problems"])

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)
            self.save_settings()

    def download(self):
        scenes = self._need_selection()
        if not scenes or not self.require_sign_in():
            return
        cfg = self.cfg()
        clip = self.clip.isChecked() and cfg["streamable"]
        bbox = None
        if clip:
            try:
                bbox, _ = self.get_aoi()
            except Exception as e:
                self.warn(str(e))
                return
        out_dir = self.out_dir.text().strip() or os.path.join(os.path.expanduser("~"), "EarthStream")
        self.save_settings()
        work = tasks.download_work(self.platform.currentData(), scenes, bbox, clip, out_dir)
        self.run_task("EarthStream download", work, self._download_done)

    def _download_done(self, result):
        if self.add_downloads.isChecked():
            self._add_specs(dict(specs=result["specs"], problems=list(result["problems"])))
        elif result["problems"]:
            self.report_problems(result["problems"])
        self.set_status("Saved %d file(s) to %s" % (len(result["written"]), result["out_dir"]))
        self.iface.messageBar().pushMessage("EarthStream", "Saved %d file(s) to %s"
                                            % (len(result["written"]), result["out_dir"]),
                                            level=msg_level("Success"), duration=8)

    def clear_layers(self):
        proj = QgsProject.instance()
        for lid in self.layer_ids:
            if proj.mapLayer(lid):
                proj.removeMapLayer(lid)
        self.layer_ids = []
        self._clear_results()
        root = proj.layerTreeRoot()
        grp = root.findGroup(layers.GROUP_NAME)
        if grp is not None and not grp.children():
            root.removeChildNode(grp)
        self.iface.mapCanvas().refresh()
        self.set_status("Cleared.")

    # ===== analysis ===========================================================================

    def analyze(self):
        scenes = self._need_selection()
        if not scenes or not self.require_sign_in():
            return
        if not (self.want_composite.isChecked() or self.want_series.isChecked()):
            self.warn("Tick at least one output (composite raster or time series).")
            return
        try:
            bbox, aoi_path = self.get_aoi()
        except Exception as e:
            self.warn(str(e))
            return
        self.save_settings()
        options = dict(mask_clouds=self.mask_clouds.isChecked() and self.mask_clouds.isEnabled(),
                       mask_snow=self.mask_snow.isChecked() and self.mask_snow.isEnabled(),
                       mask_water=self.mask_water.isChecked() and self.mask_water.isEnabled())
        out_dir = os.path.join(self.out_dir.text().strip() or os.path.join(os.path.expanduser("~"), "EarthStream"),
                               "analysis")
        work = tasks.analyze_work(self.platform.currentData(), self.product.currentText(), scenes, bbox,
                                  self.res.value(), self.method.currentText(), options, out_dir, aoi_path,
                                  self.want_composite.isChecked(), self.want_series.isChecked())
        self.run_task("EarthStream analysis", work, self._analysis_done)

    def _analysis_done(self, res):
        spec = res["spec"]
        method = res.get("method", "")
        if res.get("composite"):
            name = "%s %s" % (res["product"], method.lower()) if method else res["product"]
            ramp, invert, rng = spec.get("ramp", "Spectral"), spec.get("invert", False), spec.get("range")
            if method == "Valid count":
                ramp, invert, rng = "Viridis", False, (0, max(1, res["observations"]))
            elif method == "Std. deviation" and rng:
                ramp, invert, rng = "Viridis", False, (0, (rng[1] - rng[0]) * 0.25)
            elif method.startswith("Change"):
                span = (rng[1] - rng[0]) * 0.5 if rng else None
                rng = (-span, span) if span else None
                if res["product"].startswith("Land surface"):
                    ramp, invert = "RdBu", True
                elif spec.get("units") in ("%",):
                    ramp, invert = "BrBG", False
                else:
                    ramp, invert = "RdYlGn", False
            layer, err = layers.add_raster(dict(path=res["composite"], name=name, style="pseudo", ramp=ramp,
                                                invert=invert, range=rng))
            if layer:
                self.layer_ids.append(layer.id())
        if res.get("csv"):
            uri = QUrl.fromLocalFile(res["csv"]).toString() + "?type=csv&delimiter=,&detectTypes=yes&geomType=none"
            table = QgsVectorLayer(uri, "%s time series" % res["product"], "delimitedtext")
            if table.isValid():
                layers.add_to_group(table, top=False)
                self.layer_ids.append(table.id())
        msg = "Analysis finished: %d observation(s) at %g m." % (res["observations"], res["grid_res"])
        self.set_status(msg)
        notes = list(res.get("notes") or [])
        if res.get("skipped"):
            notes.append("Skipped %d granule(s); details are in the log panel (EarthStream)." % len(res["skipped"]))
            for s in res["skipped"]:
                QgsMessageLog.logMessage("Skipped " + s, "EarthStream", msg_level("Warning"))
        self.iface.messageBar().pushMessage("EarthStream", " ".join([msg] + notes), level=msg_level("Success"),
                                            duration=10)
        if res.get("png"):
            self.show_chart(res["png"], res["csv"])

    def show_chart(self, png, csv_path):
        dlg = QDialog(self)
        dlg.setWindowTitle("Time series")
        lay = QVBoxLayout(dlg)
        pic = QLabel()
        pic.setPixmap(QPixmap(png))
        lay.addWidget(pic)
        path = QLabel("Table: %s" % csv_path)
        path.setWordWrap(True)
        lay.addWidget(path)
        row = QHBoxLayout()
        b_open = QPushButton("Open Folder")
        b_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(png))))
        b_close = QPushButton("Close")
        b_close.clicked.connect(dlg.accept)
        row.addStretch(1)
        row.addWidget(b_open)
        row.addWidget(b_close)
        lay.addLayout(row)
        dlg.setModal(False)
        dlg.show()
        self._chart = dlg  # keep a reference so it isn't collected

    # ===== task plumbing ======================================================================

    def run_task(self, description, work, on_done):
        if self.task is not None:
            self.warn("Another EarthStream job is still running.")
            return
        task = tasks.EarthTask(description, work, lambda r: self._task_done(on_done, r), self._task_failed)
        task.statusChanged.connect(self.set_status)
        task.progressChanged.connect(lambda v: self.progress.setValue(int(v)))
        self.task = task
        self.set_busy(True)
        QgsApplication.taskManager().addTask(task)

    def _task_done(self, on_done, result):
        self.task = None
        self.set_busy(False)
        try:
            on_done(result)
        except Exception as e:
            import traceback
            QgsMessageLog.logMessage(traceback.format_exc(), "EarthStream", msg_level("Critical"))
            self.warn(str(e))

    def _task_failed(self, error, canceled, trace):
        self.task = None
        self.set_busy(False)
        if canceled or error is None:
            self.set_status("Canceled.")
            return
        if trace:
            QgsMessageLog.logMessage(trace, "EarthStream", msg_level("Critical"))
        self.set_status("Failed: %s" % error)
        self.iface.messageBar().pushMessage("EarthStream", str(error), level=msg_level("Critical"), duration=12)
        if isinstance(error, credentials.AuthError):
            self._update_auth_label()

    def cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.set_status("Canceling...")

    def set_busy(self, busy):
        for b in self.action_buttons:
            b.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        if not busy:
            self.progress.setValue(0)
            self.on_platform_buttons()

    def on_platform_buttons(self):
        cfg = self.cfg()
        self.btn_visual.setEnabled(cfg["streamable"])
        self.btn_ms.setEnabled(bool(cfg.get("ms_bands")))
        self.btn_analyze.setEnabled(cfg["streamable"] and self.product.count() > 0)

    # ===== messages ===========================================================================

    def set_status(self, text):
        self.status.setText(text)

    def warn(self, text):
        self.iface.messageBar().pushMessage("EarthStream", text, level=msg_level("Warning"), duration=8)
        self.set_status(text)

    def info(self, text):
        self.iface.messageBar().pushMessage("EarthStream", text, level=msg_level("Info"), duration=8)
        self.set_status(text)

    def report_problems(self, problems):
        for p in problems:
            QgsMessageLog.logMessage(p, "EarthStream", msg_level("Warning"))
        first = problems[0]
        more = " (and %d more in the log panel)" % (len(problems) - 1) if len(problems) > 1 else ""
        self.iface.messageBar().pushMessage("EarthStream", first + more, level=msg_level("Warning"), duration=10)

    # ===== teardown ===========================================================================

    def shutdown(self):
        self.save_settings()
        if self.task is not None:
            self.task.cancel()
        credentials.release_gdal()  # take the token back out of GDAL when the plugin unloads
        proj = QgsProject.instance()
        try:
            proj.layersAdded.disconnect(self.refresh_aoi_layers)
            proj.layersRemoved.disconnect(self.refresh_aoi_layers)
        except TypeError:
            pass

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)
