"""PCG Scatter Tool: builds a PCG scatter graph for a landscape and the kitbash meshes on it.

Inside Unreal (Output Log, Python):   import pcg_scatter; pcg_scatter.launch()
Outside Unreal (presets only):        python -m pcg_scatter
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any

__version__ = "0.1.0"

_window: Any = None
_tick_handle: Any = None

INSTALL_HELP = (
    "The PCG Scatter Tool needs PySide6 (or PyQt6) in Unreal's Python.\n\n"
    "Close the editor and run this in a command prompt (adjust both paths):\n\n"
    '"C:\\Program Files\\Epic Games\\UE_5.6\\Engine\\Binaries\\ThirdParty\\Python3\\Win64\\python.exe" '
    '-m pip install --target "D:\\MyProject\\Content\\Python\\Lib\\site-packages" PySide6\n\n'
    "The README has the details."
)

# Reload order matters: each module is reloaded after the modules it imports.
_SUBMODULES = ("model", "plan", "backends", "qt_compat", "unreal_backend", "ui", "menu")


def _add_project_site_packages(unreal: Any) -> None:
    """Makes packages installed with pip --target into the project importable."""
    try:
        content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
        intermediate = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_intermediate_dir())
    except Exception:
        return
    for root in (os.path.join(content, "Python", "Lib", "site-packages"),
                 os.path.join(intermediate, "PipInstall", "Lib", "site-packages")):
        if os.path.isdir(root) and root not in sys.path:
            sys.path.append(root)


def _reload_submodules() -> None:
    for name in _SUBMODULES:
        module = sys.modules.get(f"{__name__}.{name}")
        if module is not None:
            importlib.reload(module)


def launch(reload: bool = False) -> Any:
    """Opens the tool window in the Unreal Editor. Use reload=True after updating the tool's files."""
    import unreal

    _add_project_site_packages(unreal)
    if reload:
        _reload_submodules()
    try:
        from .qt_compat import QtWidgets
    except ImportError as exc:
        unreal.log_error(str(exc))
        unreal.EditorDialog.show_message("PCG Scatter Tool", INSTALL_HELP, unreal.AppMsgType.OK)
        return None
    from .ui import ScatterToolWindow
    from .unreal_backend import UnrealBackend

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["UnrealEditor"])
    app.setQuitOnLastWindowClosed(False)

    global _window
    if _window is not None:
        try:
            _window.close()
            _window.deleteLater()
        except RuntimeError:  # already deleted on the C++ side
            pass
    _window = ScatterToolWindow(UnrealBackend())
    _window.show()
    try:
        unreal.parent_external_window_to_slate(int(_window.winId()))
    except Exception:
        pass  # still usable, just not parented to the editor window
    _keep_qt_responsive(unreal, app)
    return _window


def _keep_qt_responsive(unreal: Any, app: Any) -> None:
    """Unreal owns the main loop, so let Qt handle its events on every Slate tick."""
    global _tick_handle
    if _tick_handle is not None:
        return
    busy = [False]

    def tick(_delta_seconds: float) -> None:
        if busy[0]:  # a Qt event started work that ticks Slate again; don't re-enter
            return
        busy[0] = True
        try:
            app.processEvents()
        finally:
            busy[0] = False

    _tick_handle = unreal.register_slate_post_tick_callback(tick)
