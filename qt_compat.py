# Small helpers so the same code runs on QGIS 3 (Qt5) and QGIS 4 (Qt6).
# Qt6 only accepts fully scoped enums, and QGIS moved several enums into Qgis.*
# over the 3.x series, so every lookup here tries the new spelling first.

from qgis.core import Qgis, QgsField, QgsTask


def enum(owner, scoped, legacy=None):
    """Return owner.Scope.Member if it exists, else owner.Member."""
    obj = owner
    try:
        for part in scoped.split("."):
            obj = getattr(obj, part)
        return obj
    except AttributeError:
        name = legacy or scoped.split(".")[-1]
        return getattr(owner, name)


def msg_level(name):
    # name is one of Info, Warning, Critical, Success
    try:
        return getattr(Qgis.MessageLevel, name)
    except AttributeError:
        return getattr(Qgis, name)


def task_can_cancel():
    try:
        return QgsTask.Flag.CanCancel
    except AttributeError:
        return QgsTask.CanCancel


def make_field(name, kind="str"):
    # QGIS 3.38+ wants QMetaType; older builds want QVariant.
    try:
        from qgis.PyQt.QtCore import QMetaType
        types = {
            "str": QMetaType.Type.QString,
            "int": QMetaType.Type.Int,
            "float": QMetaType.Type.Double,
        }
        return QgsField(name, types[kind])
    except (ImportError, AttributeError, TypeError):
        from qgis.PyQt.QtCore import QVariant
        types = {"str": QVariant.String, "int": QVariant.Int, "float": QVariant.Double}
        return QgsField(name, types[kind])


def exec_dialog(dlg):
    # exec_ was removed in PyQt6
    return dlg.exec() if hasattr(dlg, "exec") else dlg.exec_()
