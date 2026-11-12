# Turns layer specs from the worker tasks into styled QGIS layers.

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsGradientColorRamp,
    QgsHillshadeRenderer,
    QgsMultiBandColorRenderer,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsVectorLayer,
)

from .qt_compat import enum, make_field

GROUP_NAME = "EarthStream"


def group():
    root = QgsProject.instance().layerTreeRoot()
    grp = root.findGroup(GROUP_NAME)
    if grp is None:
        grp = root.insertGroup(0, GROUP_NAME)
    return grp


def add_to_group(layer, top=True):
    QgsProject.instance().addMapLayer(layer, False)
    grp = group()
    if top:
        grp.insertLayer(0, layer)
    else:
        grp.addLayer(layer)
    return layer


def _ramp(name, invert=False):
    ramp = QgsStyle.defaultStyle().colorRamp(name)
    if ramp is None:
        ramp = QgsGradientColorRamp(QColor(68, 1, 84), QColor(253, 231, 37))
    if invert:
        try:
            ramp.invert()
        except AttributeError:
            pass
    return ramp


def pseudocolor(layer, ramp_name, vmin, vmax, invert=False, band=1, classes=11):
    ramp = _ramp(ramp_name, invert)
    items = []
    for i in range(classes):
        t = i / float(classes - 1)
        value = vmin + (vmax - vmin) * t
        items.append(QgsColorRampShader.ColorRampItem(value, ramp.color(t), "%.4g" % value))
    shader_fn = QgsColorRampShader(vmin, vmax)
    shader_fn.setColorRampItemList(items)
    try:
        shader_fn.setSourceColorRamp(ramp.clone())
    except Exception:
        pass
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(shader_fn)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band, shader)
    renderer.setClassificationMin(vmin)
    renderer.setClassificationMax(vmax)
    layer.setRenderer(renderer)


def multiband(layer, bands, vmin, vmax):
    prov = layer.dataProvider()
    r, g, b = bands
    renderer = QgsMultiBandColorRenderer(prov, r, g, b)
    algo = enum(QgsContrastEnhancement, "ContrastEnhancementAlgorithm.StretchToMinimumMaximum",
                "StretchToMinimumMaximum")
    setters = (renderer.setRedContrastEnhancement, renderer.setGreenContrastEnhancement,
               renderer.setBlueContrastEnhancement)
    for band, setter in zip((r, g, b), setters):
        ce = QgsContrastEnhancement(prov.dataType(band))
        ce.setContrastEnhancementAlgorithm(algo)
        ce.setMinimumValue(vmin)
        ce.setMaximumValue(vmax)
        setter(ce)
    layer.setRenderer(renderer)


def hillshade(layer, geographic=False):
    renderer = QgsHillshadeRenderer(layer.dataProvider(), 1, 315.0, 45.0)
    if geographic and layer.crs().isGeographic():
        # Elevation is in metres but the grid is in degrees.
        renderer.setZFactor(1.0 / 111320.0)
    try:
        renderer.setMultiDirectional(True)
    except AttributeError:
        pass
    layer.setRenderer(renderer)


def band_range(layer, band=1):
    prov = layer.dataProvider()
    try:
        st = prov.bandStatistics(band)
        return st.minimumValue, st.maximumValue
    except Exception:
        return 0.0, 1.0


def add_raster(spec):
    layer = QgsRasterLayer(spec["path"], spec["name"], "gdal")
    if not layer.isValid():
        return None, "QGIS could not open %s" % spec["name"]
    style = spec.get("style")
    if style == "rgb":
        lo, hi = spec.get("ms_range") or spec.get("range") or (0, 3000)
        multiband(layer, spec["bands"], lo, hi)
    elif style == "pseudo":
        rng = spec.get("range") or band_range(layer)
        pseudocolor(layer, spec.get("ramp", "Spectral"), rng[0], rng[1], spec.get("invert", False))
    elif style == "hillshade":
        hillshade(layer, spec.get("geographic", False))
    # "palette": the file's own colour table is used as is.
    if spec.get("opacity") is not None:
        layer.setOpacity(spec["opacity"])
    add_to_group(layer)
    return layer, None


# ----- footprints ---------------------------------------------------------------------

def footprint_layer(scenes, name="Imagery footprints"):
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", name, "memory")
    prov = layer.dataProvider()
    prov.addAttributes([make_field("granule", "str"), make_field("date", "str"),
                        make_field("cloud", "float"), make_field("tile", "str"), make_field("row", "int")])
    layer.updateFields()
    feats = []
    for idx, sc in enumerate(scenes):
        parts = []
        for ring in sc["rings"]:
            pts = [QgsPointXY(x, y) for x, y in ring]
            if pts and pts[0] != pts[-1]:
                pts.append(pts[0])
            parts.append([pts])
        if not parts:
            continue
        f = QgsFeature(layer.fields())
        geom = QgsGeometry.fromMultiPolygonXY(parts) if len(parts) > 1 else QgsGeometry.fromPolygonXY(parts[0])
        f.setGeometry(geom)
        f.setAttributes([sc["id"], sc["datetime"][:16].replace("T", " "),
                         sc["cloud"] if sc["cloud"] is not None else None, sc["tile"], idx])
        feats.append(f)
    prov.addFeatures(feats)
    layer.updateExtents()
    symbol = QgsFillSymbol.createSimple({
        "color": "0,0,0,0",
        "outline_color": "232,121,30,255",
        "outline_width": "0.45",
    })
    layer.renderer().setSymbol(symbol)
    try:
        layer.setSelectionColor(QColor(255, 214, 0, 160))
    except AttributeError:
        pass
    add_to_group(layer, top=False)
    return layer
