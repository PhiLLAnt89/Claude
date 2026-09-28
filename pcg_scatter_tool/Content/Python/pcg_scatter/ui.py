"""Qt window for the PCG Scatter Tool (PySide6 or PyQt6, through qt_compat)."""
from __future__ import annotations

import contextlib
import html
import os
import random
import traceback
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import model
from . import plan as planlib
from .backends import ERROR, INFO, WARNING, BuildReport, OfflineBackend
from .model import FlatMask, MeshEntry, ScatterLayer, SurfaceMode, ToolSettings
from .qt_compat import BINDING, QtCore, QtGui, QtWidgets, Signal, exec_, qenum

Qt = QtCore.Qt
ALIGN_CENTER = qenum(Qt, "AlignmentFlag.AlignCenter")
CHECKED = qenum(Qt, "CheckState.Checked")
UNCHECKED = qenum(Qt, "CheckState.Unchecked")
ITEM_CHECKABLE = qenum(Qt, "ItemFlag.ItemIsUserCheckable")
HORIZONTAL = qenum(Qt, "Orientation.Horizontal")
NO_PEN = qenum(Qt, "PenStyle.NoPen")
DASH_LINE = qenum(Qt, "PenStyle.DashLine")
RICH_TEXT = qenum(Qt, "TextFormat.RichText")
WAIT_CURSOR = qenum(Qt, "CursorShape.WaitCursor")
NO_EDIT = qenum(QtWidgets.QAbstractItemView, "EditTrigger.NoEditTriggers")
SELECT_ROWS = qenum(QtWidgets.QAbstractItemView, "SelectionBehavior.SelectRows")
STRETCH = qenum(QtWidgets.QHeaderView, "ResizeMode.Stretch")
FIXED = qenum(QtWidgets.QHeaderView, "ResizeMode.Fixed")
NO_FRAME = qenum(QtWidgets.QFrame, "Shape.NoFrame")
ANTIALIASING = qenum(QtGui.QPainter, "RenderHint.Antialiasing")
FIXED_FONT = qenum(QtGui.QFontDatabase, "SystemFont.FixedFont")
YES = qenum(QtWidgets.QMessageBox, "StandardButton.Yes")
NO = qenum(QtWidgets.QMessageBox, "StandardButton.No")

TITLE = "PCG Scatter Tool"
ACCENT = "#3d8ef0"
GOOD = "#5cc98a"
WARN = "#e6b450"
BAD = "#f07178"
TEXT = "#d4d7dc"
DIM = "#8b9099"

STYLE = f"""
QWidget {{ background: #202226; color: {TEXT}; }}
QMainWindow {{ background: #1a1c1f; }}
QGroupBox {{ border: 1px solid #34373d; border-radius: 5px; margin-top: 16px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9fc4ff; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QListWidget, QTableWidget {{
    background: #15171a; border: 1px solid #3a3e45; border-radius: 3px; padding: 3px 5px;
    selection-background-color: #2a5ea8; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QPushButton {{ background: #33373e; border: 1px solid #454a52; border-radius: 3px; padding: 5px 12px; }}
QPushButton:hover {{ background: #3c414a; }}
QPushButton:pressed {{ background: #2a2d33; }}
QPushButton:disabled {{ color: #6b7079; background: #26282c; border-color: #2f3237; }}
QPushButton#primary {{ background: #2f6fd0; border-color: #3d8ef0; color: white; font-weight: bold; }}
QPushButton#primary:hover {{ background: #3a7fe6; }}
QTabWidget::pane {{ border: 1px solid #34373d; border-radius: 4px; top: -1px; }}
QTabBar::tab {{ background: #1b1d20; border: 1px solid #34373d; padding: 6px 14px; margin-right: 2px;
    border-top-left-radius: 4px; border-top-right-radius: 4px; color: {DIM}; }}
QTabBar::tab:selected {{ background: #202226; border-bottom-color: #202226; color: white; }}
QHeaderView::section {{ background: #2a2d33; border: none; border-right: 1px solid #34373d; padding: 4px 6px; }}
QListWidget::item {{ padding: 6px 4px; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: #2a4f86; color: white; }}
QCheckBox::indicator, QGroupBox::indicator, QListView::indicator {{
    width: 13px; height: 13px; border: 1px solid #737a85; border-radius: 3px; background: #15171a; }}
QCheckBox::indicator:hover, QGroupBox::indicator:hover, QListView::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked, QListView::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:disabled, QGroupBox::indicator:disabled {{ border-color: #3a3e45; background: #1b1d20; }}
QCheckBox::indicator:checked:disabled, QGroupBox::indicator:checked:disabled {{ background: #2d4a73; }}
QCheckBox:disabled, QGroupBox:disabled {{ color: #6b7079; }}
QSplitter::handle {{ background: #2a2d33; }}
QLabel#hint {{ color: {DIM}; }}
QLabel#section {{ color: #9fc4ff; font-weight: bold; }}
QLabel#issues {{ background: #26221c; border: 1px solid #4a4030; border-radius: 4px; padding: 6px 8px; }}
QScrollArea {{ border: none; }}
QStatusBar {{ background: #16181b; color: {DIM}; }}
"""


# ---------------------------------------------------------------------------------------------
# Widget helpers
# ---------------------------------------------------------------------------------------------

class _WheelOnlyWhenFocused:
    """Scrolling the panel shouldn't change values the pointer happens to pass over."""

    def wheelEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _DoubleSpin(_WheelOnlyWhenFocused, QtWidgets.QDoubleSpinBox):
    pass


class _IntSpin(_WheelOnlyWhenFocused, QtWidgets.QSpinBox):
    pass


class _Combo(_WheelOnlyWhenFocused, QtWidgets.QComboBox):
    pass


def _prepare_spin(spin: Any, tip: str) -> Any:
    spin.setKeyboardTracking(False)
    spin.setButtonSymbols(qenum(QtWidgets.QAbstractSpinBox, "ButtonSymbols.NoButtons"))
    spin.setFocusPolicy(qenum(Qt, "FocusPolicy.StrongFocus"))
    spin.setToolTip(tip + ("\n" if tip else "") + "Type a value, or click and use the mouse wheel or arrow keys.")
    spin.setMinimumWidth(96)
    return spin


def _double(minimum: float, maximum: float, decimals: int = 1, step: float = 1.0,
            suffix: str = "", tip: str = "") -> QtWidgets.QDoubleSpinBox:
    spin = _DoubleSpin()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setSuffix(suffix)
    return _prepare_spin(spin, tip)


def _int(minimum: int, maximum: int, tip: str = "") -> QtWidgets.QSpinBox:
    spin = _IntSpin()
    spin.setRange(minimum, maximum)
    return _prepare_spin(spin, tip)


def _combo() -> QtWidgets.QComboBox:
    combo = _Combo()
    combo.setFocusPolicy(qenum(Qt, "FocusPolicy.StrongFocus"))
    return combo


def _hint(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def _row(*widgets: Any, stretch: bool = True) -> QtWidgets.QWidget:
    """Widgets side by side; strings become labels and None becomes a stretchable gap."""
    holder = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(QtWidgets.QLabel(widget) if isinstance(widget, str) else widget)
    if stretch:
        layout.addStretch(1)
    return holder


def _stack(*widgets: QtWidgets.QWidget) -> QtWidgets.QWidget:
    holder = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for widget in widgets:
        layout.addWidget(widget)
    return holder


def _button(text: str, slot: Callable[[], Any], tip: str = "", enabled: bool = True) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.clicked.connect(lambda *_: slot())
    button.setToolTip(tip)
    button.setEnabled(enabled)
    return button


def _dark_palette(base: QtGui.QPalette) -> QtGui.QPalette:
    """Dark colours for what the stylesheet doesn't reach (e.g. arrows the style draws itself)."""
    palette = QtGui.QPalette(base)
    roles = {
        "Window": "#202226", "WindowText": TEXT, "Base": "#15171a", "AlternateBase": "#1b1d20",
        "Text": TEXT, "Button": "#33373e", "ButtonText": TEXT, "Highlight": "#2a5ea8",
        "HighlightedText": "#ffffff", "ToolTipBase": "#2a2d33", "ToolTipText": TEXT, "PlaceholderText": DIM,
    }
    for role, color in roles.items():
        palette.setColor(qenum(QtGui.QPalette, f"ColorRole.{role}"), QtGui.QColor(color))
    return palette


def _scroll(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(NO_FRAME)
    area.setWidget(widget)
    return area


# ---------------------------------------------------------------------------------------------
# Slope strip
# ---------------------------------------------------------------------------------------------

class SlopeBar(QtWidgets.QWidget):
    """0°–90° strip showing which slopes a layer keeps after the slope and flat-area rules."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._low, self._high = 0.0, 90.0
        self._flat = FlatMask.ANY
        self._threshold = 10.0
        self.setMinimumHeight(52)
        self.setToolTip("Blue: slopes that get meshes. 0° is flat ground, 90° is a vertical wall. "
                        "The dashed line is the flat-area limit.")

    def set_state(self, low: float, high: float, flat: FlatMask, threshold: float) -> None:
        self._low, self._high, self._flat, self._threshold = low, high, flat, threshold
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(ANTIALIASING)
        track = QtCore.QRectF(12, 8, max(10, self.width() - 24), 16)

        def x_at(degrees: float) -> float:
            return track.left() + track.width() * degrees / 90.0

        painter.setPen(NO_PEN)
        painter.setBrush(QtGui.QColor("#121417"))
        painter.drawRoundedRect(track, 4, 4)
        if self._low <= self._high:
            kept = QtCore.QRectF(x_at(self._low), track.top(),
                                 max(3.0, x_at(self._high) - x_at(self._low)), track.height())
            painter.setBrush(QtGui.QColor(ACCENT))
            painter.drawRoundedRect(kept, 3, 3)
        if self._flat is not FlatMask.ANY:
            pen = QtGui.QPen(QtGui.QColor(WARN))
            pen.setStyle(DASH_LINE)
            pen.setWidth(2)
            painter.setPen(pen)
            x = x_at(self._threshold)
            painter.drawLine(QtCore.QPointF(x, track.top() - 4), QtCore.QPointF(x, track.bottom() + 4))
        painter.setPen(QtGui.QColor(DIM))
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        painter.setFont(font)
        for degrees in range(0, 91, 15):
            x = x_at(degrees)
            painter.drawLine(QtCore.QPointF(x, track.bottom() + 2), QtCore.QPointF(x, track.bottom() + 6))
            painter.drawText(QtCore.QRectF(x - 20, track.bottom() + 7, 40, 14), ALIGN_CENTER, f"{degrees}°")
        painter.end()


# ---------------------------------------------------------------------------------------------
# Mesh list
# ---------------------------------------------------------------------------------------------

class MeshTable(QtWidgets.QWidget):
    changed = Signal()

    def __init__(self, backend: Any, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self._entries: List[MeshEntry] = []

        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Static mesh", "Weight"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, STRETCH)
        header.setSectionResizeMode(1, FIXED)
        self.table.setColumnWidth(1, 96)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(NO_EDIT)
        self.table.setSelectionBehavior(SELECT_ROWS)
        self.table.setMinimumHeight(200)

        in_unreal = bool(backend.available)
        buttons = _row(
            _button("Add from Content Browser", self._add_from_browser,
                    "Adds the static meshes selected in the Content Browser.", in_unreal),
            _button("Add from selected actors", self._add_from_actors,
                    "Adds the meshes used by the actors selected in the level.", in_unreal),
            _button("Add by path…", self._add_by_path, "Type an asset path such as /Game/Env/SM_Rock_01."),
            _button("Remove", self.remove_selected),
            _button("Equal weights", self._equal_weights),
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)
        layout.addWidget(_hint("Weight sets how often a mesh is picked compared to the others in this layer."))

    def set_entries(self, entries: List[MeshEntry]) -> None:
        self._entries = entries  # the layer's own list; edits go straight into it
        self._rebuild()

    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        for row, entry in enumerate(self._entries):
            self.table.insertRow(row)
            item = QtWidgets.QTableWidgetItem(entry.display_name)
            item.setToolTip(entry.path)
            self.table.setItem(row, 0, item)
            weight = _int(0, 1000, "0 = not used")
            weight.setValue(int(entry.weight))
            weight.valueChanged.connect(lambda value, e=entry: self._set_weight(e, value))
            self.table.setCellWidget(row, 1, weight)

    def _set_weight(self, entry: MeshEntry, value: int) -> None:
        entry.weight = int(value)
        self.changed.emit()

    def add_paths(self, paths: List[str]) -> int:
        existing = {entry.path for entry in self._entries}
        added = 0
        for path in paths:
            path = str(path).strip()
            if path and path not in existing:
                self._entries.append(MeshEntry(path, 1))
                existing.add(path)
                added += 1
        if added:
            self._rebuild()
            self.changed.emit()
        return added

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            del self._entries[row]
        if rows:
            self._rebuild()
            self.changed.emit()

    def _equal_weights(self) -> None:
        for entry in self._entries:
            entry.weight = 1
        self._rebuild()
        self.changed.emit()

    def _add_from_browser(self) -> None:
        if not self.add_paths(self._backend.selected_static_mesh_paths()):
            QtWidgets.QMessageBox.information(self, TITLE, "Select one or more new static meshes in the Content Browser first.")

    def _add_from_actors(self) -> None:
        if not self.add_paths(self._backend.selected_actor_mesh_paths()):
            QtWidgets.QMessageBox.information(self, TITLE, "Select static mesh actors in the level first.")

    def _add_by_path(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(self, TITLE, "Static mesh asset path:")
        if ok and text.strip():
            self.add_paths([text])


# ---------------------------------------------------------------------------------------------
# Layer editor
# ---------------------------------------------------------------------------------------------

class LayerEditor(QtWidgets.QWidget):
    changed = Signal()
    renamed = Signal(str)

    def __init__(self, backend: Any, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self._layer: Optional[ScatterLayer] = None
        self._settings: Optional[ToolSettings] = None
        self._area_m2: Optional[float] = None
        self._loading = False
        self._bindings: List[tuple] = []
        self.fields: Dict[str, Any] = {}  # layer attribute -> widget

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._meshes_tab(), "Meshes")
        self.tabs.addTab(_scroll(self._placement_tab()), "Placement")
        self.tabs.addTab(_scroll(self._masks_tab()), "Masks")
        self.tabs.addTab(_scroll(self._transform_tab()), "Transform")
        self.tabs.addTab(_scroll(self._rendering_tab()), "Rendering")
        self.issues = QtWidgets.QLabel()
        self.issues.setObjectName("issues")
        self.issues.setWordWrap(True)
        self.issues.setTextFormat(RICH_TEXT)
        self.issues.hide()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.issues)

    # -- binding ------------------------------------------------------------------------------

    def _bind(self, attr: str, read: Callable[[], Any], write: Callable[[Any], Any], signal: Any,
              widget: Any = None) -> None:
        self._bindings.append((attr, write))
        if widget is not None:
            self.fields[attr] = widget
        signal.connect(lambda *_: self._commit(attr, read()))

    def _bind_spin(self, attr: str, spin: Any, cast: Callable[[Any], Any] = float) -> None:
        self._bind(attr, lambda: cast(spin.value()), lambda v: spin.setValue(cast(v)), spin.valueChanged, spin)

    def _bind_check(self, attr: str, box: Any) -> None:
        self._bind(attr, box.isChecked, lambda v: box.setChecked(bool(v)), box.toggled, box)

    def _bind_combo(self, attr: str, combo: QtWidgets.QComboBox, enum_type: Any) -> None:
        def write(value: Any) -> None:
            combo.setCurrentIndex(max(0, combo.findData(value.value)))

        self._bind(attr, lambda: enum_type(combo.currentData()), write, combo.currentIndexChanged, combo)

    def _commit(self, attr: str, value: Any) -> None:
        if self._loading or self._layer is None or getattr(self._layer, attr) == value:
            return
        setattr(self._layer, attr, value)
        self.refresh()
        self.changed.emit()

    # -- tabs ---------------------------------------------------------------------------------

    def _meshes_tab(self) -> QtWidgets.QWidget:
        self.meshes = MeshTable(self._backend)
        self.meshes.changed.connect(self._on_meshes_changed)
        return self.meshes

    def _placement_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)

        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.textEdited.connect(self._on_name_edited)
        self._bindings.append(("name", self.name_edit.setText))
        self.fields["name"] = self.name_edit
        form.addRow("Layer name", self.name_edit)

        self.surface_combo = _combo()
        for mode in SurfaceMode:
            self.surface_combo.addItem(mode.label, mode.value)
        self._bind_combo("surface", self.surface_combo, SurfaceMode)
        self.surface_help = _hint("")
        form.addRow("Surface", _stack(self.surface_combo, self.surface_help))

        density = _double(0.0001, 100.0, 4, 0.01, " /m²", "Candidate points per square metre, before masks.")
        self._bind_spin("density", density)
        self.estimate = _hint("")
        form.addRow("Density", _stack(_row(density), self.estimate))

        seed = _int(0, 2_147_483_647, "Change it to get a different random layout.")
        self._bind_spin("seed", seed, int)
        form.addRow("Seed", _row(seed, _button("Random", lambda: seed.setValue(random.randint(1, 999_999)))))

        radius = _double(1.0, 100_000.0, 0, 10.0, " cm",
                         "Space each point claims: used for spacing and by later layers that avoid this one.")
        self._bind_spin("footprint_radius", radius)
        form.addRow("Footprint radius", _row(radius))

        prune = QtWidgets.QCheckBox("Remove overlapping points (minimum spacing = footprint)")
        self._bind_check("prune_overlaps", prune)
        avoid_layers = QtWidgets.QCheckBox("Keep clear of the layers above this one")
        avoid_layers.setToolTip("Removes points whose footprint overlaps a footprint from an earlier layer, "
                                "so the gap is roughly both radii added together.")
        self._bind_check("avoid_previous_layers", avoid_layers)
        self.avoid_kitbash = QtWidgets.QCheckBox("Keep off kitbash meshes (Landscape only surface)")
        self._bind_check("avoid_kitbash", self.avoid_kitbash)
        form.addRow("Spacing", prune)
        form.addRow("", avoid_layers)
        form.addRow("", self.avoid_kitbash)
        return page

    def _masks_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(page)

        slope = QtWidgets.QGroupBox("Slope")
        form = QtWidgets.QFormLayout(slope)
        slope_min = _double(0.0, 90.0, 1, 1.0, "°")
        slope_max = _double(0.0, 90.0, 1, 1.0, "°")
        self._bind_spin("slope_min", slope_min)
        self._bind_spin("slope_max", slope_max)
        form.addRow("Allowed slope", _row("from", slope_min, "to", slope_max))
        self.slope_bar = SlopeBar()
        form.addRow(self.slope_bar)
        self.slope_summary = _hint("")
        form.addRow(self.slope_summary)
        column.addWidget(slope)

        flat = QtWidgets.QGroupBox("Flat areas")
        form = QtWidgets.QFormLayout(flat)
        flat_combo = _combo()
        for mode in FlatMask:
            flat_combo.addItem(mode.label, mode.value)
        self._bind_combo("flat_mask", flat_combo, FlatMask)
        self.threshold = _double(0.0, 90.0, 1, 1.0, "°", "Ground flatter than this counts as flat.")
        self._bind_spin("flat_threshold", self.threshold)
        form.addRow("Place on", _row(flat_combo))
        form.addRow("Flat means below", _row(self.threshold))
        form.addRow(_hint("Works together with the slope range; the strip above shows the result."))
        column.addWidget(flat)

        height = QtWidgets.QGroupBox("Height range")
        height.setCheckable(True)
        self._bind_check("use_height", height)
        form = QtWidgets.QFormLayout(height)
        height_min = _double(-1e7, 1e7, 0, 100.0, " cm")
        height_max = _double(-1e7, 1e7, 0, 100.0, " cm")
        self._bind_spin("height_min", height_min)
        self._bind_spin("height_max", height_max)
        form.addRow("World Z between", _row(height_min, "and", height_max))
        column.addWidget(height)

        self.paint_group = QtWidgets.QGroupBox("Landscape paint layer")
        self.paint_group.setCheckable(True)
        self._bind_check("use_layer_mask", self.paint_group)
        form = QtWidgets.QFormLayout(self.paint_group)
        self.layer_combo = _combo()
        self.layer_combo.setEditable(True)
        self.layer_combo.setMinimumWidth(180)
        self.layer_combo.currentTextChanged.connect(lambda text: self._commit("layer_name", str(text).strip()))
        self._bindings.append(("layer_name", self.layer_combo.setCurrentText))
        self.fields["layer_name"] = self.layer_combo
        weight = _double(0.0, 1.0, 2, 0.05, "", "Painted weight needed (0–1).")
        self._bind_spin("layer_min_weight", weight)
        form.addRow("Layer", _row(self.layer_combo))
        form.addRow("Minimum weight", _row(weight))
        column.addWidget(self.paint_group)
        self.paint_note = _hint("Paint-layer masks need Surface = Landscape only (Placement tab).")
        column.addWidget(self.paint_note)

        patches = QtWidgets.QGroupBox("Patches")
        patches.setCheckable(True)
        self._bind_check("use_patches", patches)
        form = QtWidgets.QFormLayout(patches)
        size = _double(100.0, 1e6, 0, 100.0, " cm", "Rough size of each clump.")
        self._bind_spin("patch_size", size)
        coverage = _double(0.0, 100.0, 0, 5.0, " %", "How much of the allowed area the clumps cover.")
        self._bind("patch_coverage", lambda: coverage.value() / 100.0,
                   lambda v: coverage.setValue(float(v) * 100.0), coverage.valueChanged, coverage)
        form.addRow("Patch size", _row(size))
        form.addRow("Coverage", _row(coverage))
        form.addRow(_hint("Noise breaks the layer into clumps instead of an even spread."))
        column.addWidget(patches)
        column.addStretch(1)
        return page

    def _transform_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        scale_min = _double(0.01, 100.0, 2, 0.05)
        scale_max = _double(0.01, 100.0, 2, 0.05)
        self._bind_spin("scale_min", scale_min)
        self._bind_spin("scale_max", scale_max)
        form.addRow("Scale", _row("from", scale_min, "to", scale_max))

        yaw = QtWidgets.QCheckBox("Random rotation around the up axis")
        self._bind_check("random_yaw", yaw)
        form.addRow("Rotation", yaw)
        tilt = _double(0.0, 90.0, 1, 1.0, "°")
        self._bind_spin("max_tilt", tilt)
        form.addRow("Random tilt up to", _row(tilt))
        align = QtWidgets.QCheckBox("Align to the surface (off = always upright)")
        self._bind_check("align_to_surface", align)
        form.addRow("Orientation", align)

        z_min = _double(-100_000.0, 100_000.0, 1, 1.0, " cm")
        z_max = _double(-100_000.0, 100_000.0, 1, 1.0, " cm")
        self._bind_spin("z_offset_min", z_min)
        self._bind_spin("z_offset_max", z_max)
        form.addRow("Z offset", _row("from", z_min, "to", z_max))
        form.addRow(_hint("Negative offsets sink meshes into the ground (rocks, tree roots). "
                          "With alignment on, the offset follows the surface."))
        return page

    def _rendering_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        cull_start = _double(0.0, 1e7, 0, 100.0, " cm")
        cull_end = _double(0.0, 1e7, 0, 100.0, " cm")
        self._bind_spin("cull_start", cull_start)
        self._bind_spin("cull_end", cull_end)
        form.addRow("Cull distance", _row("fade from", cull_start, "to", cull_end))
        form.addRow(_hint("Leave the end at 0 to never cull."))
        collision = QtWidgets.QCheckBox("Collision")
        shadows = QtWidgets.QCheckBox("Cast shadows")
        self._bind_check("collision", collision)
        self._bind_check("cast_shadows", shadows)
        form.addRow("Instances", collision)
        form.addRow("", shadows)
        form.addRow(_hint("Turning collision off on grass and small debris saves memory and physics time."))
        return page

    # -- state --------------------------------------------------------------------------------

    def set_layer(self, layer: Optional[ScatterLayer], settings: ToolSettings) -> None:
        self._layer, self._settings = layer, settings
        self.setEnabled(layer is not None)
        if layer is None:
            self.issues.hide()
            return
        self._loading = True
        try:
            for attr, write in self._bindings:
                write(getattr(layer, attr))
            self.meshes.set_entries(layer.meshes)
        finally:
            self._loading = False
        self.refresh()

    def set_area(self, area_m2: Optional[float]) -> None:
        self._area_m2 = area_m2
        self.refresh()

    def set_layer_names(self, names: List[str]) -> None:
        current = self.layer_combo.currentText()
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        self.layer_combo.addItems(names)
        self.layer_combo.setCurrentText(current)
        self.layer_combo.blockSignals(False)

    def refresh(self) -> None:
        layer = self._layer
        if layer is None:
            return
        low, high = model.effective_slope_range(layer)
        self.slope_bar.set_state(low, high, layer.flat_mask, layer.flat_threshold)
        if low <= high:
            self.slope_summary.setText(f"Meshes go on slopes from {low:g}° to {high:g}°.")
        else:
            self.slope_summary.setText("No slope is left. Widen the range or change the flat-area rule.")
        self.threshold.setEnabled(layer.flat_mask is not FlatMask.ANY)

        landscape_only = layer.surface is SurfaceMode.LANDSCAPE_ONLY
        self.surface_help.setText(layer.surface.help)
        self.avoid_kitbash.setEnabled(landscape_only)
        self.paint_group.setEnabled(landscape_only)
        self.paint_note.setVisible(not landscape_only)

        samples = model.estimate_samples(layer, self._area_m2)
        if samples is None:
            self.estimate.setText("Pick a landscape to see how many points this makes.")
        elif samples > model.HEAVY_SAMPLE_COUNT:
            self.estimate.setText(f"About {samples:,} candidate points before masks. That's heavy: lower the "
                                  "density or turn on partitioned generation.")
        else:
            self.estimate.setText(f"About {samples:,} candidate points on this landscape before masks.")

        issues = model.validate_layer(layer, self._settings)
        if issues:
            lines = []
            for issue in issues:
                color, mark = (BAD, "✖") if issue.level == "error" else (WARN, "⚠")
                lines.append(f'<span style="color:{color}">{mark} {html.escape(issue.message)}</span>')
            self.issues.setText("<br>".join(lines))
        self.issues.setVisible(bool(issues))

    def _on_name_edited(self, text: str) -> None:
        self._commit("name", text)
        self.renamed.emit(text)

    def _on_meshes_changed(self) -> None:
        if not self._loading:
            self.refresh()
            self.changed.emit()


# ---------------------------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------------------------

class ScatterToolWindow(QtWidgets.QMainWindow):
    def __init__(self, backend: Any = None, settings: Optional[ToolSettings] = None,
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.backend = backend or OfflineBackend()
        self.settings = settings or self._load_session() or model.default_settings()
        self.setWindowTitle(TITLE)
        self.setObjectName("PCGScatterToolWindow")
        self.resize(1120, 900)
        self.setPalette(_dark_palette(self.palette()))
        self.setStyleSheet(STYLE)

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addWidget(self._target_group())

        self.editor = LayerEditor(self.backend)
        self.editor.changed.connect(self._refresh_layer_labels)
        self.editor.renamed.connect(lambda *_: self._refresh_layer_labels())
        columns = QtWidgets.QSplitter(HORIZONTAL)
        columns.addWidget(self._layers_panel())
        columns.addWidget(self.editor)
        columns.setStretchFactor(1, 1)
        columns.setSizes([230, 880])
        work = QtWidgets.QWidget()
        work_layout = QtWidgets.QVBoxLayout(work)
        work_layout.setContentsMargins(0, 0, 0, 4)
        work_layout.addWidget(columns, 1)
        work_layout.addWidget(self._actions())

        rows = QtWidgets.QSplitter(qenum(Qt, "Orientation.Vertical"))
        rows.addWidget(work)
        rows.addWidget(self._log_panel())
        rows.setStretchFactor(0, 1)
        rows.setSizes([620, 170])
        layout.addWidget(rows, 1)
        self.setCentralWidget(central)

        if self.backend.available:
            mode = f"Unreal Engine {self.backend.engine_version()}"
        else:
            mode = "Offline: presets only. Open the tool inside Unreal to build."
        self.statusBar().showMessage(f"{mode}   ·   Qt binding: {BINDING}")

        self._apply_target_fields()
        self.refresh_landscapes()
        self._rebuild_layer_list(select=0)

    # -- layout -------------------------------------------------------------------------------

    def _target_group(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("Target")
        grid = QtWidgets.QGridLayout(box)
        grid.setColumnStretch(1, 1)
        in_unreal = bool(self.backend.available)

        self.landscape_combo = _combo()
        self.landscape_combo.currentIndexChanged.connect(lambda *_: self._on_landscape_changed())
        self.landscape_size = _hint("")
        grid.addWidget(QtWidgets.QLabel("Landscape"), 0, 0)
        grid.addWidget(self.landscape_combo, 0, 1)
        grid.addWidget(_row(
            _button("Refresh", self.refresh_landscapes, "Re-read the landscapes in the open level.", in_unreal),
            _button("Use selected", self._use_selected_landscape, "Use the landscape selected in the level.", in_unreal),
            self.landscape_size, stretch=False), 0, 2)

        self.graph_edit = QtWidgets.QLineEdit()
        self.graph_edit.setToolTip("The tool creates this PCG graph and rebuilds it on every build.")
        grid.addWidget(QtWidgets.QLabel("PCG graph"), 1, 0)
        grid.addWidget(self.graph_edit, 1, 1)
        grid.addWidget(_hint("Created and rebuilt by the tool."), 1, 2)

        self.fit_check = QtWidgets.QCheckBox("Fit PCG volume to landscape")
        self.margin_spin = _double(0.0, 1e6, 0, 100.0, " cm", "Extra room above and below the landscape and kitbash meshes.")
        self.partition_check = QtWidgets.QCheckBox("Partitioned (large worlds)")
        self.partition_check.setToolTip("Generate in World Partition cells instead of one big component.")
        grid.addWidget(QtWidgets.QLabel("Volume"), 2, 0)
        grid.addWidget(_row(self.fit_check, "height margin", self.margin_spin, self.partition_check), 2, 1, 1, 2)

        self.kitbash_edit = QtWidgets.QLineEdit()
        self.kitbash_edit.setToolTip("Actor tag that marks kitbash meshes (used by 'Kitbash meshes only' and "
                                     "'keep off kitbash meshes').")
        grid.addWidget(QtWidgets.QLabel("Kitbash tag"), 3, 0)
        grid.addWidget(self.kitbash_edit, 3, 1)
        grid.addWidget(_row(
            _button("Tag selected", lambda: self._tag(self.kitbash_edit, True), "Add the tag to the selected actors.", in_unreal),
            _button("Untag selected", lambda: self._tag(self.kitbash_edit, False), "", in_unreal),
            stretch=False), 3, 2)

        self.no_scatter_edit = QtWidgets.QLineEdit()
        self.no_scatter_edit.setToolTip("Rays pass through actors with this tag (buildings, props, water...).")
        grid.addWidget(QtWidgets.QLabel("No-scatter tag"), 4, 0)
        grid.addWidget(self.no_scatter_edit, 4, 1)
        grid.addWidget(_row(
            _button("Tag selected", lambda: self._tag(self.no_scatter_edit, True), "Add the tag to the selected actors.", in_unreal),
            _button("Untag selected", lambda: self._tag(self.no_scatter_edit, False), "", in_unreal),
            stretch=False), 4, 2)

        self.complex_check = QtWidgets.QCheckBox("Trace complex collision (exact kitbash surfaces, slower)")
        grid.addWidget(self.complex_check, 5, 1, 1, 2)

        for edit in (self.graph_edit, self.kitbash_edit, self.no_scatter_edit):
            edit.editingFinished.connect(self._read_target_fields)
        for check in (self.fit_check, self.partition_check, self.complex_check):
            check.toggled.connect(lambda *_: self._read_target_fields())
        self.margin_spin.valueChanged.connect(lambda *_: self._read_target_fields())
        return box

    def _layers_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel("Layers")
        title.setObjectName("section")
        column.addWidget(title)
        self.layer_list = QtWidgets.QListWidget()
        self.layer_list.currentRowChanged.connect(self._on_layer_selected)
        self.layer_list.itemChanged.connect(self._on_layer_item_changed)
        column.addWidget(self.layer_list, 1)
        column.addWidget(_row(_button("Add", self.add_layer), _button("Duplicate", self._duplicate_layer),
                              _button("Remove", self._remove_layer), stretch=False))
        column.addWidget(_row(_button("Move up", lambda: self._move_layer(-1)),
                              _button("Move down", lambda: self._move_layer(1)), stretch=False))
        column.addWidget(_hint("Built top to bottom. Tick a layer to include it."))
        return panel

    def _actions(self) -> QtWidgets.QWidget:
        in_unreal = bool(self.backend.available)
        self.build_button = _button("Build graph", lambda: self.build(False),
                                    "Create or update the PCG graph and volume without generating.", in_unreal)
        self.generate_button = _button("Build && generate", lambda: self.build(True),
                                       "Build, then generate the meshes.", in_unreal)
        self.generate_button.setObjectName("primary")
        return _row(
            _button("Load preset…", self._load_preset),
            _button("Save preset…", self._save_preset),
            _button("Preview graph", self.preview, "List the nodes the build will create."),
            _button("Check PCG API", self._diagnostics,
                    "Test this engine's PCG Python API without touching your level.", in_unreal),
            None,
            _button("Clean up", self._cleanup, "Remove the generated meshes.", in_unreal),
            self.build_button,
            self.generate_button,
            stretch=False,
        )

    def _log_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        title = QtWidgets.QLabel("Log")
        title.setObjectName("section")
        column.addWidget(_row(title, None,
                              _button("Copy", self._copy_log, "Copy the log to the clipboard."),
                              _button("Clear", lambda: self.log_view.clear()), stretch=False))
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QtGui.QFontDatabase.systemFont(FIXED_FONT))
        self.log_view.setMinimumHeight(70)
        column.addWidget(self.log_view)
        return panel

    # -- target -------------------------------------------------------------------------------

    def _apply_target_fields(self) -> None:
        s = self.settings
        widgets = (self.graph_edit, self.kitbash_edit, self.no_scatter_edit, self.fit_check,
                   self.partition_check, self.complex_check, self.margin_spin)
        for widget in widgets:
            widget.blockSignals(True)
        self.graph_edit.setText(s.graph_path)
        self.kitbash_edit.setText(s.kitbash_tag)
        self.no_scatter_edit.setText(s.no_scatter_tag)
        self.fit_check.setChecked(s.fit_volume)
        self.partition_check.setChecked(s.partitioned)
        self.complex_check.setChecked(s.trace_complex)
        self.margin_spin.setValue(s.volume_margin)
        for widget in widgets:
            widget.blockSignals(False)

    def _read_target_fields(self) -> None:
        s = self.settings
        s.graph_path = self.graph_edit.text().strip()
        s.kitbash_tag = self.kitbash_edit.text().strip()
        s.no_scatter_tag = self.no_scatter_edit.text().strip()
        s.fit_volume = self.fit_check.isChecked()
        s.partitioned = self.partition_check.isChecked()
        s.trace_complex = self.complex_check.isChecked()
        s.volume_margin = float(self.margin_spin.value())
        self.editor.refresh()
        self._refresh_layer_labels()

    def refresh_landscapes(self) -> None:
        infos = self.backend.list_landscapes() if self.backend.available else []
        combo = self.landscape_combo
        combo.blockSignals(True)
        combo.clear()
        if not infos:
            combo.addItem("(no landscape in the open level)" if self.backend.available else "(open inside Unreal)", "")
        for info in infos:
            combo.addItem(info.label, info.id)
        index = combo.findData(self.settings.landscape) if self.settings.landscape else -1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        self._on_landscape_changed()

    def _on_landscape_changed(self) -> None:
        landscape_id = str(self.landscape_combo.currentData() or "")
        if self.backend.available:
            self.settings.landscape = landscape_id
        bounds = self.backend.landscape_bounds(landscape_id) if landscape_id else None
        self.editor.set_area(bounds.area_m2 if bounds else None)
        self.editor.set_layer_names(self.backend.landscape_layer_names(landscape_id) if landscape_id else [])
        self.landscape_size.setText(f"{bounds.size[0] / 1e5:.2f} x {bounds.size[1] / 1e5:.2f} km" if bounds else "")

    def _use_selected_landscape(self) -> None:
        landscape_id = self.backend.selected_landscape_id()
        if not landscape_id:
            QtWidgets.QMessageBox.information(self, TITLE, "Select a landscape (or one of its proxies) in the level first.")
            return
        if self.landscape_combo.findData(landscape_id) < 0:
            self.refresh_landscapes()
        index = self.landscape_combo.findData(landscape_id)
        if index >= 0:
            self.landscape_combo.setCurrentIndex(index)

    def _tag(self, edit: QtWidgets.QLineEdit, add: bool) -> None:
        self._read_target_fields()
        tag = edit.text().strip()
        if not tag:
            QtWidgets.QMessageBox.information(self, TITLE, "Type a tag first.")
            return
        try:
            count = self.backend.tag_selected_actors(tag, add)
        except Exception as exc:
            self.log(str(exc), ERROR)
            return
        verb = "Tagged" if add else "Untagged"
        self.log(f"{verb} {count} selected actor(s) with '{tag}'.", "ok" if count else WARNING)

    # -- layers -------------------------------------------------------------------------------

    def _layer_label(self, layer: ScatterLayer) -> str:
        broken = any(issue.level == "error" for issue in model.validate_layer(layer, self.settings))
        return ("⚠ " if broken and layer.enabled else "") + (layer.name or "(unnamed)")

    def _rebuild_layer_list(self, select: Optional[int] = None) -> None:
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for layer in self.settings.layers:
            item = QtWidgets.QListWidgetItem(self._layer_label(layer))
            item.setFlags(item.flags() | ITEM_CHECKABLE)
            item.setCheckState(CHECKED if layer.enabled else UNCHECKED)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)
        layers = self.settings.layers
        if layers:
            row = 0 if select is None else max(0, min(select, len(layers) - 1))
            self.layer_list.setCurrentRow(row)
            self._on_layer_selected(row)
        else:
            self.editor.set_layer(None, self.settings)

    def _refresh_layer_labels(self) -> None:
        self.layer_list.blockSignals(True)
        for row, layer in enumerate(self.settings.layers):
            item = self.layer_list.item(row)
            if item is not None:
                item.setText(self._layer_label(layer))
        self.layer_list.blockSignals(False)

    def current_row(self) -> int:
        return self.layer_list.currentRow()

    def _on_layer_selected(self, row: int) -> None:
        layers = self.settings.layers
        self.editor.set_layer(layers[row] if 0 <= row < len(layers) else None, self.settings)

    def _on_layer_item_changed(self, item: QtWidgets.QListWidgetItem) -> None:
        row = self.layer_list.row(item)
        if 0 <= row < len(self.settings.layers):
            self.settings.layers[row].enabled = item.checkState() == CHECKED
            self._refresh_layer_labels()

    def add_layer(self) -> None:
        self.settings.layers.append(ScatterLayer(name=f"Layer {len(self.settings.layers) + 1}",
                                                 seed=random.randint(1, 999_999)))
        self._rebuild_layer_list(select=len(self.settings.layers) - 1)

    def _duplicate_layer(self) -> None:
        row = self.current_row()
        if row < 0:
            return
        duplicate = model.copy_layer(self.settings.layers[row])
        duplicate.name += " copy"
        duplicate.seed += 1
        self.settings.layers.insert(row + 1, duplicate)
        self._rebuild_layer_list(select=row + 1)

    def _remove_layer(self) -> None:
        row = self.current_row()
        if row < 0:
            return
        layer = self.settings.layers[row]
        if layer.meshes:
            answer = QtWidgets.QMessageBox.question(self, TITLE, f"Remove layer '{layer.name}'?", YES | NO)
            if answer != YES:
                return
        del self.settings.layers[row]
        self._rebuild_layer_list(select=row)

    def _move_layer(self, delta: int) -> None:
        row, layers = self.current_row(), self.settings.layers
        target = row + delta
        if 0 <= row < len(layers) and 0 <= target < len(layers):
            layers[row], layers[target] = layers[target], layers[row]
            self._rebuild_layer_list(select=target)

    # -- actions ------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _busy(self) -> Iterator[None]:
        QtWidgets.QApplication.setOverrideCursor(QtGui.QCursor(WAIT_CURSOR))
        try:
            yield
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def preview(self) -> str:
        self._read_target_fields()
        text = planlib.build_plan(self.settings).describe()
        self.log("Graph preview", "head")
        self.log(text)
        return text

    def build(self, generate: bool) -> Optional[BuildReport]:
        self._read_target_fields()
        errors = [issue.message for issue in model.validate_settings(self.settings, self.backend.available)
                  if issue.level == "error"]
        if errors:
            for message in errors:
                self.log(message, ERROR)
            QtWidgets.QMessageBox.warning(self, TITLE, "\n".join(errors))
            return None
        self.log("Build and generate" if generate else "Build graph", "head")
        with self._busy():
            try:
                report = self.backend.build(self.settings, generate=generate)
            except Exception:
                self.log(traceback.format_exc(), ERROR)
                self.log("The build stopped with an error. Copy the log and send it along.", ERROR)
                return None
        self.show_report(report)
        self.save_session()
        return report

    def _cleanup(self) -> None:
        self._read_target_fields()
        self.log("Clean up", "head")
        try:
            self.show_report(self.backend.cleanup(self.settings))
        except Exception:
            self.log(traceback.format_exc(), ERROR)

    def _diagnostics(self) -> None:
        self.log("PCG API check", "head")
        with self._busy():
            try:
                text = self.backend.diagnostics()
            except Exception:
                text = "ERROR: " + traceback.format_exc()
        for line in text.splitlines():
            level = ERROR if line.startswith("ERROR") else WARNING if line.startswith("WARNING") else INFO
            self.log(line, level)
        self.log("If something failed, press Copy and send the log along.", "ok")

    def show_report(self, report: BuildReport) -> None:
        for level, text in report.visible_lines():
            self.log(text, level)
        self.log(report.summary(), ERROR if report.errors else WARNING if report.warnings else "ok")
        self.statusBar().showMessage(report.summary(), 10000)

    def log(self, text: str, level: str = INFO) -> None:
        color = {INFO: TEXT, WARNING: WARN, ERROR: BAD, "ok": GOOD, "head": ACCENT}.get(level, TEXT)
        weight = "font-weight:bold;" if level == "head" else ""
        for line in (str(text).splitlines() or [""]):
            escaped = html.escape(line) or "&nbsp;"
            self.log_view.appendHtml(f'<span style="color:{color};{weight}white-space:pre">{escaped}</span>')

    def _copy_log(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.log_view.toPlainText())
        self.statusBar().showMessage("Log copied to the clipboard.", 4000)

    # -- presets and session ------------------------------------------------------------------

    def _preset_dir(self) -> str:
        return os.path.dirname(self.backend.session_path())

    def set_settings(self, settings: ToolSettings) -> None:
        if not settings.landscape:
            settings.landscape = self.settings.landscape
        self.settings = settings
        self._apply_target_fields()
        self.refresh_landscapes()
        self._rebuild_layer_list(select=0)

    def _load_preset(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load preset", self._preset_dir(), "Scatter presets (*.json)")
        if not path:
            return
        try:
            self.set_settings(model.load_settings(path))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, TITLE, f"Could not read {path}:\n{exc}")
            return
        self.log(f"Loaded preset {path}", "ok")

    def _save_preset(self) -> None:
        self._read_target_fields()
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save preset", self._preset_dir(), "Scatter presets (*.json)")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            model.save_settings(self.settings, path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, TITLE, f"Could not save {path}:\n{exc}")
            return
        self.log(f"Saved preset {path}", "ok")

    def _load_session(self) -> Optional[ToolSettings]:
        try:
            path = self.backend.session_path()
            return model.load_settings(path) if os.path.isfile(path) else None
        except Exception:
            return None

    def save_session(self) -> None:
        try:
            path = self.backend.session_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            model.save_settings(self.settings, path)
        except Exception as exc:
            self.log(f"Could not save the session: {exc}", WARNING)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        self._read_target_fields()
        self.save_session()
        super().closeEvent(event)


def run_standalone(argv: Optional[List[str]] = None) -> int:
    """Opens the window outside Unreal (preset editing only)."""
    import sys

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(list(argv or sys.argv))
    window = ScatterToolWindow(OfflineBackend())
    window.show()
    return exec_(app)
