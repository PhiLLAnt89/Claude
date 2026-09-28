"""Loads whichever Qt binding is installed: PySide6, PyQt6, PySide2 or PyQt5 (in that order).

Set the environment variable PCG_SCATTER_QT (e.g. to "PyQt6") to force one.
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Tuple

CANDIDATES = ("PySide6", "PyQt6", "PySide2", "PyQt5")


def _load() -> Tuple[str, Any, Any, Any]:
    forced = os.environ.get("PCG_SCATTER_QT", "").strip()
    order = (forced,) if forced else CANDIDATES
    errors = []
    for name in order:
        try:
            core = importlib.import_module(name + ".QtCore")
            gui = importlib.import_module(name + ".QtGui")
            widgets = importlib.import_module(name + ".QtWidgets")
            return name, core, gui, widgets
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
    raise ImportError(
        "No Qt binding found for the PCG Scatter Tool. Install PySide6 (or PyQt6) into Unreal's "
        "Python, as described in the README.\n" + "\n".join(errors)
    )


BINDING, QtCore, QtGui, QtWidgets = _load()
QT_VERSION = QtCore.qVersion()
Signal = getattr(QtCore, "Signal", None) or getattr(QtCore, "pyqtSignal")


def qenum(scope: Any, dotted: str) -> Any:
    """Enum lookup for Qt6 (scoped) and Qt5 (flat) bindings, e.g. qenum(Qt, "AlignmentFlag.AlignLeft")."""
    obj = scope
    try:
        for part in dotted.split("."):
            obj = getattr(obj, part)
        return obj
    except AttributeError:
        return getattr(scope, dotted.rsplit(".", 1)[-1])


def exec_(obj: Any, *args: Any) -> Any:
    method = getattr(obj, "exec", None) or getattr(obj, "exec_")
    return method(*args)
