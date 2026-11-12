# QGIS entry point.


def classFactory(iface):
    from .earthstream import EarthStreamPlugin
    return EarthStreamPlugin(iface)
