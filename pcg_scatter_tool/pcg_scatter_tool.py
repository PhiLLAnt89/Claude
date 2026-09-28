"""PCG Scatter Tool for Unreal Engine 5.6, in one file.

Builds a PCG graph from lists of static meshes and fills a landscape with them, both on the landscape
and on top of kitbash meshes placed on it. Each layer has controls for slope, flat areas, height,
paint layers, noise patches, spacing, scale/rotation/offset and culling.

Run it inside Unreal from the Output Log, with the input box set to "Cmd":
    py "D:/Tools/pcg_scatter_tool.py"

It needs PySide6 (or PyQt6) in Unreal's Python. Close the editor and run (adjust both paths):
    "C:/Program Files/Epic Games/UE_5.6/Engine/Binaries/ThirdParty/Python3/Win64/python.exe" -m pip install
        --target "D:/MyProject/Content/Python/Lib/site-packages" PySide6
and enable these plugins: Procedural Content Generation Framework, Python Editor Script Plugin,
Editor Scripting Utilities.

First run: press "Check PCG API". If a line says FAILED, WARNING or ERROR, press Copy and send the log.
Outside Unreal, `python pcg_scatter_tool.py` opens the window for editing presets only.
"""
from __future__ import annotations

import contextlib
import html
import importlib
import json
import math
import os
import random
import re
import sys
import traceback
import types
from collections import OrderedDict
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

try:
    import unreal  # only exists inside the Unreal Editor
except ImportError:
    unreal = None

__version__ = "0.2.0"

INSTALL_HELP = (
    "The PCG Scatter Tool needs PySide6 (or PyQt6) in Unreal's Python.\n\n"
    "Close the editor and run this in a command prompt (adjust both paths):\n\n"
    '"C:\\Program Files\\Epic Games\\UE_5.6\\Engine\\Binaries\\ThirdParty\\Python3\\Win64\\python.exe" '
    '-m pip install --target "D:\\MyProject\\Content\\Python\\Lib\\site-packages" PySide6'
)


# =============================================================================================
# Qt binding
# =============================================================================================

QT_BINDINGS = ("PySide6", "PyQt6", "PySide2", "PyQt5")


def _add_project_site_packages() -> None:
    """Makes packages installed with pip --target into the project importable."""
    if unreal is None:
        return
    try:
        content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
        intermediate = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_intermediate_dir())
    except Exception:
        return
    for root in (os.path.join(content, "Python", "Lib", "site-packages"),
                 os.path.join(intermediate, "PipInstall", "Lib", "site-packages")):
        if os.path.isdir(root) and root not in sys.path:
            sys.path.append(root)


def _load_qt() -> Tuple[str, Any, Any, Any]:
    """PySide6, PyQt6, PySide2 or PyQt5, in that order. Set PCG_SCATTER_QT to force one."""
    forced = os.environ.get("PCG_SCATTER_QT", "").strip()
    order = (forced,) if forced else QT_BINDINGS
    errors = []
    for name in order:
        try:
            core = importlib.import_module(name + ".QtCore")
            gui = importlib.import_module(name + ".QtGui")
            widgets = importlib.import_module(name + ".QtWidgets")
            return name, core, gui, widgets
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
    raise ImportError("No Qt binding found for the PCG Scatter Tool (tried " + ", ".join(order) + ").\n"
                      + INSTALL_HELP + "\n" + "\n".join(errors))


_add_project_site_packages()
try:
    BINDING, QtCore, QtGui, QtWidgets = _load_qt()
except ImportError as _qt_error:
    if unreal is not None:
        unreal.log_error(str(_qt_error))
        try:
            unreal.EditorDialog.show_message("PCG Scatter Tool", INSTALL_HELP, unreal.AppMsgType.OK)
        except Exception:
            pass
    raise

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


# =============================================================================================
# Settings, layers, validation and presets (no Unreal or Qt needed)
# =============================================================================================

SCHEMA_VERSION = 1

# Above this many candidate points a layer is slow to generate on one PCG component.
HEAVY_SAMPLE_COUNT = 3_000_000


class SurfaceMode(str, Enum):
    """Which geometry a layer scatters on."""

    LANDSCAPE_AND_KITBASH = "landscape_and_kitbash"
    LANDSCAPE_ONLY = "landscape_only"
    KITBASH_ONLY = "kitbash_only"

    @property
    def label(self) -> str:
        return _SURFACE_LABELS[self]

    @property
    def help(self) -> str:
        return _SURFACE_HELP[self]


_SURFACE_LABELS = {
    SurfaceMode.LANDSCAPE_AND_KITBASH: "Landscape + kitbash meshes",
    SurfaceMode.LANDSCAPE_ONLY: "Landscape only",
    SurfaceMode.KITBASH_ONLY: "Kitbash meshes only",
}

_SURFACE_HELP = {
    SurfaceMode.LANDSCAPE_AND_KITBASH: (
        "Rays are cast straight down from above, so points land on whatever is on top: the tops of "
        "kitbash meshes where they sit on the landscape, the landscape everywhere else. Actors tagged "
        "with the no-scatter tag are ignored. Kitbash meshes need collision."
    ),
    SurfaceMode.LANDSCAPE_ONLY: (
        "Samples the landscape directly. Supports paint-layer masks and can keep points away from "
        "actors carrying the kitbash tag."
    ),
    SurfaceMode.KITBASH_ONLY: (
        "Only the tops of actors carrying the kitbash tag, e.g. moss or debris on rock formations. "
        "Kitbash meshes need collision."
    ),
}


class FlatMask(str, Enum):
    """Extra slope rule on top of the slope range."""

    ANY = "any"
    ONLY_FLAT = "only_flat"
    NO_FLAT = "no_flat"

    @property
    def label(self) -> str:
        return _FLAT_LABELS[self]


_FLAT_LABELS = {
    FlatMask.ANY: "Flat and sloped ground",
    FlatMask.ONLY_FLAT: "Only flat areas",
    FlatMask.NO_FLAT: "No flat areas",
}


@dataclass
class MeshEntry:
    path: str
    weight: int = 1

    @property
    def display_name(self) -> str:
        return self.path.rsplit("/", 1)[-1].split(".")[0] or self.path


@dataclass
class ScatterLayer:
    name: str = "Layer"
    enabled: bool = True
    meshes: List[MeshEntry] = field(default_factory=list)

    # Placement
    surface: SurfaceMode = SurfaceMode.LANDSCAPE_AND_KITBASH
    density: float = 0.05  # points per m² before masks
    seed: int = 1
    footprint_radius: float = 100.0  # cm; spacing and the area later layers keep clear of
    prune_overlaps: bool = True
    avoid_previous_layers: bool = True
    avoid_kitbash: bool = True  # landscape-only mode

    # Masks
    slope_min: float = 0.0
    slope_max: float = 35.0
    flat_mask: FlatMask = FlatMask.ANY
    flat_threshold: float = 10.0
    use_height: bool = False
    height_min: float = -50000.0
    height_max: float = 50000.0
    use_layer_mask: bool = False
    layer_name: str = ""
    layer_min_weight: float = 0.5
    use_patches: bool = False
    patch_size: float = 3000.0  # cm
    patch_coverage: float = 0.5  # 0..1

    # Transform
    scale_min: float = 0.8
    scale_max: float = 1.2
    random_yaw: bool = True
    max_tilt: float = 0.0
    align_to_surface: bool = False
    z_offset_min: float = 0.0
    z_offset_max: float = 0.0

    # Rendering
    cull_start: float = 0.0  # cm, 0 = never cull
    cull_end: float = 0.0
    collision: bool = True
    cast_shadows: bool = True


@dataclass
class ToolSettings:
    graph_path: str = "/Game/PCG/PCG_Scatter"
    landscape: str = ""  # landscape actor path name
    kitbash_tag: str = "PCG_Kitbash"
    no_scatter_tag: str = "PCG_NoScatter"
    trace_complex: bool = False
    fit_volume: bool = True
    volume_margin: float = 5000.0  # cm above/below the landscape and kitbash meshes
    partitioned: bool = False
    generate_after_build: bool = True
    layers: List[ScatterLayer] = field(default_factory=list)


@dataclass(frozen=True)
class Bounds:
    """Axis-aligned box in world space (cm)."""

    min: Tuple[float, float, float]
    max: Tuple[float, float, float]

    @classmethod
    def from_origin_extent(cls, origin, extent) -> "Bounds":
        o = tuple(float(v) for v in origin)
        e = tuple(abs(float(v)) for v in extent)
        return cls(tuple(o[i] - e[i] for i in range(3)), tuple(o[i] + e[i] for i in range(3)))

    @property
    def center(self) -> Tuple[float, float, float]:
        return tuple((self.min[i] + self.max[i]) * 0.5 for i in range(3))

    @property
    def extent(self) -> Tuple[float, float, float]:
        return tuple((self.max[i] - self.min[i]) * 0.5 for i in range(3))

    @property
    def size(self) -> Tuple[float, float, float]:
        return tuple(self.max[i] - self.min[i] for i in range(3))

    @property
    def area_m2(self) -> float:
        return (self.size[0] / 100.0) * (self.size[1] / 100.0)

    def union(self, other: Optional["Bounds"]) -> "Bounds":
        if other is None:
            return self
        return Bounds(
            tuple(min(self.min[i], other.min[i]) for i in range(3)),
            tuple(max(self.max[i], other.max[i]) for i in range(3)),
        )

    def overlaps_xy(self, other: "Bounds") -> bool:
        return all(self.min[i] <= other.max[i] and other.min[i] <= self.max[i] for i in range(2))


@dataclass(frozen=True)
class Issue:
    level: str  # "error" or "warning"
    message: str


# ---------------------------------------------------------------------------------------------
# Slope maths
# ---------------------------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def effective_slope_range(layer: ScatterLayer) -> Tuple[float, float]:
    """Slope range (degrees) left after combining the slope limits with the flat-area rule.

    The result can be empty (min > max) when the two rules contradict each other.
    """
    low, high = sorted((clamp(layer.slope_min, 0.0, 90.0), clamp(layer.slope_max, 0.0, 90.0)))
    threshold = clamp(layer.flat_threshold, 0.0, 90.0)
    if layer.flat_mask is FlatMask.ONLY_FLAT:
        high = min(high, threshold)
    elif layer.flat_mask is FlatMask.NO_FLAT:
        low = max(low, threshold)
    return low, high


def slope_is_limited(low: float, high: float) -> bool:
    return low > 0.0 or high < 90.0


def slope_density_bounds(low: float, high: float) -> Tuple[float, float]:
    """Density range matching a slope range.

    Normal To Density with normal +Z, offset 0 and strength 1 writes cos(slope) into $Density,
    so slope in [low, high] is the same as density in [cos(high), cos(low)].
    """
    lower = max(0.0, math.cos(math.radians(high)) - 1e-6)
    upper = min(1.0, math.cos(math.radians(low)) + 1e-6)
    return lower, upper


def estimate_samples(layer: ScatterLayer, area_m2: Optional[float]) -> Optional[int]:
    if not area_m2:
        return None
    return int(area_m2 * max(layer.density, 0.0))


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------

_ASSET_PATH_RE = re.compile(r"^/[A-Za-z0-9_]+(?:/[A-Za-z0-9_\-]+)*/[A-Za-z0-9_]+$")


def split_asset_path(path: str) -> Tuple[str, str]:
    """'/Game/PCG/PCG_Scatter' (or '/Game/PCG/PCG_Scatter.PCG_Scatter') -> ('/Game/PCG', 'PCG_Scatter')."""
    cleaned = path.strip()
    if "." in cleaned.rsplit("/", 1)[-1]:
        cleaned = cleaned.rsplit(".", 1)[0]
    if not _ASSET_PATH_RE.match(cleaned):
        raise ValueError(
            f"'{path}' is not a valid asset path. Use something like /Game/PCG/PCG_Scatter "
            "(letters, numbers and underscores)."
        )
    folder, name = cleaned.rsplit("/", 1)
    return folder, name


def validate_layer(layer: ScatterLayer, settings: Optional[ToolSettings] = None) -> List[Issue]:
    issues: List[Issue] = []

    def error(message: str) -> None:
        issues.append(Issue("error", message))

    def warning(message: str) -> None:
        issues.append(Issue("warning", message))

    meshes = [m for m in layer.meshes if m.path.strip()]
    if not meshes:
        error("Add at least one static mesh.")
    elif not any(m.weight > 0 for m in meshes):
        error("All mesh weights are 0.")
    paths = [m.path for m in meshes]
    if len(set(paths)) != len(paths):
        warning("The same mesh is listed more than once.")

    if layer.density <= 0:
        error("Density must be above 0.")

    low, high = effective_slope_range(layer)
    if low > high:
        error(
            f"Slope {layer.slope_min:g}°–{layer.slope_max:g}° and the flat-area rule "
            f"({layer.flat_threshold:g}°) leave no valid slope."
        )

    if layer.use_height and layer.height_min >= layer.height_max:
        error("Height range: minimum must be below maximum.")

    if layer.use_layer_mask:
        if not layer.layer_name.strip():
            error("Paint-layer mask is on but no layer name is set.")
        elif layer.surface is not SurfaceMode.LANDSCAPE_ONLY:
            warning("Paint-layer masks only work with Surface = Landscape only; it will be ignored.")

    if layer.use_patches and layer.patch_coverage <= 0:
        warning("Patch coverage is 0%, so nothing will spawn.")

    kitbash_tag = settings.kitbash_tag.strip() if settings else "x"
    if layer.surface is SurfaceMode.KITBASH_ONLY and not kitbash_tag:
        error("Kitbash-only layers need a kitbash tag (Target section).")
    if layer.surface is SurfaceMode.LANDSCAPE_ONLY and layer.avoid_kitbash and not kitbash_tag:
        warning("No kitbash tag set, so 'keep off kitbash meshes' is ignored.")

    if layer.scale_min > layer.scale_max:
        warning("Scale min is larger than max; they will be swapped.")
    if layer.scale_min <= 0 or layer.scale_max <= 0:
        error("Scale must be above 0.")
    if layer.z_offset_min > layer.z_offset_max:
        warning("Z offset min is larger than max; they will be swapped.")
    if layer.cull_end > 0 and layer.cull_start > layer.cull_end:
        warning("Cull start is beyond cull end.")
    return issues


def validate_settings(settings: ToolSettings, require_landscape: bool = True) -> List[Issue]:
    """Checks the global (non-layer) settings."""
    issues: List[Issue] = []
    try:
        split_asset_path(settings.graph_path)
    except ValueError as exc:
        issues.append(Issue("error", str(exc)))
    if require_landscape and settings.fit_volume and not settings.landscape:
        issues.append(Issue("error", "Pick a landscape (or turn off 'Fit PCG volume to landscape')."))
    if not any(layer.enabled for layer in settings.layers):
        issues.append(Issue("error", "No enabled layers."))
    tags = [settings.kitbash_tag.strip(), settings.no_scatter_tag.strip()]
    if tags[0] and tags[0] == tags[1]:
        issues.append(Issue("error", "The kitbash tag and the no-scatter tag must be different."))
    return issues


# ---------------------------------------------------------------------------------------------
# Presets / serialization
# ---------------------------------------------------------------------------------------------

def _coerce(name: str, default: Any, raw: Any) -> Any:
    if name == "meshes":
        entries = []
        for item in raw or []:
            if isinstance(item, dict) and str(item.get("path", "")).strip():
                entries.append(MeshEntry(str(item["path"]).strip(), max(0, int(item.get("weight", 1)))))
        return entries
    if isinstance(default, Enum):
        return type(default)(raw)
    if isinstance(default, bool):
        return bool(raw)
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, str):
        return str(raw)
    return raw


def _to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, MeshEntry):
        return {"path": value.path, "weight": value.weight}
    return value


def layer_to_dict(layer: ScatterLayer) -> Dict[str, Any]:
    return {f.name: _to_plain(getattr(layer, f.name)) for f in fields(ScatterLayer)}


def layer_from_dict(data: Dict[str, Any]) -> ScatterLayer:
    layer = ScatterLayer()
    for f in fields(ScatterLayer):
        if f.name in data:
            try:
                setattr(layer, f.name, _coerce(f.name, getattr(layer, f.name), data[f.name]))
            except (TypeError, ValueError, KeyError):
                pass  # keep the default for unreadable values
    return layer


def settings_to_dict(settings: ToolSettings) -> Dict[str, Any]:
    data: Dict[str, Any] = {"schema": SCHEMA_VERSION, "tool": "pcg_scatter"}
    for f in fields(ToolSettings):
        if f.name == "layers":
            data["layers"] = [layer_to_dict(layer) for layer in settings.layers]
        else:
            data[f.name] = _to_plain(getattr(settings, f.name))
    return data


def settings_from_dict(data: Dict[str, Any]) -> ToolSettings:
    if not isinstance(data, dict):
        raise ValueError("Preset file does not contain a settings object.")
    settings = ToolSettings()
    for f in fields(ToolSettings):
        if f.name == "layers" or f.name not in data:
            continue
        try:
            setattr(settings, f.name, _coerce(f.name, getattr(settings, f.name), data[f.name]))
        except (TypeError, ValueError):
            pass
    settings.layers = [layer_from_dict(item) for item in data.get("layers", []) if isinstance(item, dict)]
    return settings


def save_settings(settings: ToolSettings, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(settings_to_dict(settings), handle, indent=2)


def load_settings(path: str) -> ToolSettings:
    with open(path, "r", encoding="utf-8") as handle:
        return settings_from_dict(json.load(handle))


def copy_layer(layer: ScatterLayer) -> ScatterLayer:
    return layer_from_dict(layer_to_dict(layer))


# ---------------------------------------------------------------------------------------------
# Starting points
# ---------------------------------------------------------------------------------------------

def default_settings() -> ToolSettings:
    """Three example layers (no meshes yet) that show the main controls."""
    trees = ScatterLayer(
        name="Trees", seed=101, density=0.01, footprint_radius=400.0,
        slope_min=0.0, slope_max=30.0, scale_min=0.8, scale_max=1.3, max_tilt=3.0,
        align_to_surface=False, z_offset_min=-20.0, z_offset_max=-5.0,
    )
    rocks = ScatterLayer(
        name="Rocks", seed=202, density=0.02, footprint_radius=150.0,
        slope_min=15.0, slope_max=70.0, scale_min=0.6, scale_max=1.5, max_tilt=15.0,
        align_to_surface=True, z_offset_min=-40.0, z_offset_max=-10.0,
    )
    grass = ScatterLayer(
        name="Grass", seed=303, surface=SurfaceMode.LANDSCAPE_ONLY, density=0.5,
        footprint_radius=25.0, prune_overlaps=False, avoid_previous_layers=False, slope_max=40.0,
        flat_mask=FlatMask.ONLY_FLAT, flat_threshold=20.0, use_patches=True,
        patch_size=2000.0, patch_coverage=0.6, scale_min=0.7, scale_max=1.2,
        align_to_surface=True, cull_start=4000.0, cull_end=6000.0, collision=False,
        cast_shadows=False,
    )
    return ToolSettings(layers=[trees, rocks, grass])


def diagnostic_settings() -> ToolSettings:
    """Every feature switched on, using an engine mesh; used by the 'Check PCG API' button."""
    cube = MeshEntry("/Engine/BasicShapes/Cube.Cube", 1)
    base = ScatterLayer(name="Check A", meshes=[cube], use_height=True, use_patches=True, max_tilt=5.0)
    landscape = ScatterLayer(
        name="Check B", meshes=[cube], surface=SurfaceMode.LANDSCAPE_ONLY, use_layer_mask=True,
        layer_name="Grass", flat_mask=FlatMask.NO_FLAT, slope_max=60.0, cull_end=5000.0, collision=False,
    )
    kitbash = ScatterLayer(name="Check C", meshes=[cube], surface=SurfaceMode.KITBASH_ONLY)
    return ToolSettings(graph_path="/Game/PCGScatterCheck/PCG_Check", layers=[base, landscape, kitbash])



# =============================================================================================
# Graph plan: settings -> PCG nodes and edges, with fallbacks per engine version
# =============================================================================================

# ---------------------------------------------------------------------------------------------
# Values (converted to Unreal types by the backend)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Vec:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Rot:
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0


@dataclass(frozen=True)
class Xform:
    location: Vec = Vec(0.0, 0.0, 0.0)
    rotation: Rot = Rot()
    scale: Vec = Vec(1.0, 1.0, 1.0)


@dataclass(frozen=True)
class EnumValue:
    enum_names: Tuple[str, ...]  # candidate Python enum type names, e.g. ("PCGDifferenceMode",)
    members: Tuple[str, ...]  # candidate member names, e.g. ("DISCRETE",)


@dataclass(frozen=True)
class NameValue:
    text: str


@dataclass(frozen=True)
class NameSet:
    names: Tuple[str, ...]


@dataclass(frozen=True)
class Selector:
    """A PCG attribute selector: '$Density' for a point property, 'Grass' for an attribute."""

    text: str


@dataclass(frozen=True)
class Box:
    min: Tuple[float, float, float]
    max: Tuple[float, float, float]


@dataclass(frozen=True)
class PointBoxes:
    """A list of world-space boxes, created as PCG points with hard (steepness 1) bounds."""

    boxes: Tuple[Box, ...]


@dataclass
class Prop:
    path: str  # dotted for nested structs; '|' separates alternatives
    value: Any
    required: bool = True


# ---------------------------------------------------------------------------------------------
# Graph description
# ---------------------------------------------------------------------------------------------

IN = "@in"  # the node's main input pin (resolved per variant)
OUT = "@out"  # the node's main output pin
INPUT_NODE = "@input"  # the graph's own Input node

PinRef = Union[str, Tuple[str, ...]]

SURFACE_PIN = ("Surface", "In")
BOUNDING_SHAPE_PIN = ("Bounding Shape", "BoundingShape")
INPUT_NODE_PINS = ("Input", "In")
DIFFERENCE_SOURCE_PIN = ("Source",)
DIFFERENCES_PIN = ("Differences",)
FILTER_INSIDE_PIN = ("InsideFilter", "Inside Filter", "In Filter", "InFilter")

ATTRIBUTE_RANGE_CLASSES = (
    "PCGAttributeFilteringRangeSettings",
    "PCGAttributeFilterRangeSettings",
    "PCGPointFilterRangeSettings",
)

COLUMN_WIDTH = 360
ROW_HEIGHT = 560
HELPER_OFFSET = 230


@dataclass
class Variant:
    classes: Tuple[str, ...]
    props: List[Prop] = field(default_factory=list)
    main_in: Tuple[str, ...] = ("In",)
    main_out: Tuple[str, ...] = ("Out",)


@dataclass
class MeshSpec:
    path: str
    weight: int
    cull_start: float
    cull_end: float
    collision: bool
    cast_shadows: bool


@dataclass
class NodeSpec:
    key: str
    title: str
    variants: List[Variant]
    summary: str = ""
    feature: str = ""  # user-facing feature this node implements
    optional: bool = False  # the executor may skip it (and bridge its neighbours)
    requires: Tuple[str, ...] = ()  # skip this node too if any of these could not be created
    meshes: Optional[List[MeshSpec]] = None
    pos: Tuple[int, int] = (0, 0)
    layer: str = ""


@dataclass
class EdgeSpec:
    src: str
    src_pin: PinRef
    dst: str
    dst_pin: PinRef


@dataclass
class GraphPlan:
    nodes: List[NodeSpec] = field(default_factory=list)
    edges: List[EdgeSpec] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    layers: List[str] = field(default_factory=list)

    def node(self, key: str) -> NodeSpec:
        for spec in self.nodes:
            if spec.key == key:
                return spec
        raise KeyError(key)

    def class_names(self) -> List[str]:
        names: List[str] = []
        for spec in self.nodes:
            for variant in spec.variants:
                for name in variant.classes:
                    if name not in names:
                        names.append(name)
        return names

    def describe(self) -> str:
        """Readable outline of the graph for the UI preview."""
        if not self.nodes:
            lines = ["Nothing to build."]
        else:
            lines = []
            groups: "OrderedDict[str, List[NodeSpec]]" = OrderedDict()
            for spec in self.nodes:
                groups.setdefault(spec.layer, []).append(spec)
            for index, (layer, specs) in enumerate(groups.items(), start=1):
                lines.append(f"Layer {index}: {layer}")
                for spec in specs:
                    detail = f" — {spec.summary}" if spec.summary else ""
                    lines.append(f"    {spec.title}{detail}")
                lines.append("")
            lines.append(f"{len(self.nodes)} nodes, {len(self.edges)} connections.")
        if self.notes:
            lines.append("")
            lines.extend(f"Note: {note}" for note in self.notes)
        return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------------------------
# Reusable node variants
# ---------------------------------------------------------------------------------------------

def _enum(enum_name: str, *members: str) -> EnumValue:
    return EnumValue((enum_name,), tuple(members))


def attribute_range_props(selector: Selector, low: float, high: float) -> List[Prop]:
    props = [Prop("target_attribute", selector)]
    for side, value in (("min_threshold", low), ("max_threshold", high)):
        props += [
            Prop(f"{side}.use_constant_threshold", True),
            Prop(f"{side}.attribute_types.type", _enum("PCGMetadataTypes", "DOUBLE")),
            Prop(f"{side}.attribute_types.double_value", float(value)),
            Prop(f"{side}.inclusive", True, required=False),
        ]
    return props


def density_range_variants(low: float, high: float) -> List[Variant]:
    """Keep points whose $Density lies in [low, high].

    Prefers the classic Density Filter node, then falls back to the generic attribute range filter.
    """
    return [
        Variant(
            ("PCGDensityFilterSettings",),
            [
                Prop("lower_bound", float(low)),
                Prop("upper_bound", float(high)),
                Prop("invert_filter", False, required=False),
            ],
        ),
        Variant(
            ATTRIBUTE_RANGE_CLASSES,
            attribute_range_props(Selector("$Density"), low, high),
            main_out=FILTER_INSIDE_PIN,
        ),
    ]


def difference_variants() -> List[Variant]:
    return [
        Variant(
            ("PCGDifferenceSettings",),
            [
                Prop("density_function", _enum("PCGDifferenceDensityFunction", "BINARY")),
                Prop("mode", _enum("PCGDifferenceMode", "DISCRETE"), required=False),
            ],
            main_in=DIFFERENCE_SOURCE_PIN,
        )
    ]


# ---------------------------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------------------------

def build_plan(settings: ToolSettings) -> GraphPlan:
    plan = GraphPlan()
    exclusion_sources: List[str] = []
    row = 0
    for layer in settings.layers:
        if not layer.enabled:
            continue
        errors = [issue for issue in validate_layer(layer, settings) if issue.level == "error"]
        if errors:
            plan.notes.extend(f"Layer '{layer.name}' skipped: {issue.message}" for issue in errors)
            continue
        builder = _LayerBuilder(plan, settings, layer, row, list(exclusion_sources))
        exclusion_sources.append(builder.build())
        plan.layers.append(layer.name)
        row += 1
    return plan


class _LayerBuilder:
    def __init__(self, plan: GraphPlan, settings: ToolSettings, layer: ScatterLayer, row: int,
                 exclusion_sources: List[str]):
        self.plan = plan
        self.settings = settings
        self.layer = layer
        self.row = row
        self.prefix = f"L{row + 1}"
        self.exclusion_sources = exclusion_sources
        self.column = 0
        self.stream: Optional[str] = None  # key of the node whose main output carries the points

    # -- helpers ------------------------------------------------------------------------------

    def _add(self, spec: NodeSpec, chain: bool = True, helper: bool = False) -> NodeSpec:
        spec.key = f"{self.prefix}.{spec.key}"
        spec.layer = self.layer.name
        y = self.row * ROW_HEIGHT
        if helper:
            spec.pos = (self.column * COLUMN_WIDTH, y + HELPER_OFFSET)
        else:
            spec.pos = (self.column * COLUMN_WIDTH, y)
            self.column += 1
        self.plan.nodes.append(spec)
        if chain:
            if self.stream is not None:
                self.plan.edges.append(EdgeSpec(self.stream, OUT, spec.key, IN))
            self.stream = spec.key
        return spec

    def _edge(self, src: str, src_pin: PinRef, dst: str, dst_pin: PinRef) -> None:
        self.plan.edges.append(EdgeSpec(src, src_pin, dst, dst_pin))

    # -- steps --------------------------------------------------------------------------------

    def build(self) -> str:
        self._surface()
        self._sampler()
        self._slope()
        self._height()
        self._paint_layer()
        self._patches()
        self._keep_off_kitbash()
        self._spacing()
        self._avoid_earlier_layers()
        final_points = self.stream
        self._transform()
        self._spawner()
        assert final_points is not None
        return final_points

    def _surface(self) -> None:
        layer, settings = self.layer, self.settings
        if layer.surface is SurfaceMode.LANDSCAPE_ONLY:
            self._add(NodeSpec(
                "surface", "Get Landscape Data",
                [Variant(("PCGGetLandscapeSettings",), [
                    Prop("sampling_properties.get_height_only", False, required=False),
                    Prop("sampling_properties.get_layer_weights", True, required=False),
                ])],
                summary="landscape surface with paint-layer weights",
            ))
            return

        kitbash_only = layer.surface is SurfaceMode.KITBASH_ONLY
        if kitbash_only:
            tag_filter, tags = "INCLUDE_TAGGED", (settings.kitbash_tag.strip(),)
            summary = f"downward rays hitting actors tagged '{tags[0]}'"
        elif settings.no_scatter_tag.strip():
            tag_filter, tags = "EXCLUDE_TAGGED", (settings.no_scatter_tag.strip(),)
            summary = f"downward rays hitting the landscape and meshes (ignores '{tags[0]}')"
        else:
            tag_filter, tags = "NO_TAG_FILTER", ()
            summary = "downward rays hitting the landscape and meshes"
        props = [
            Prop("query_params.actor_tag_filter", _enum("PCGWorldQueryFilterByTag", tag_filter)),
            Prop("query_params.ignore_pcg_hits", True, required=False),
            Prop("query_params.ignore_self_hits", True, required=False),
            Prop("query_params.trace_complex", bool(settings.trace_complex), required=False),
            Prop("query_params.select_landscape_hits",
                 _enum("PCGWorldQuerySelectLandscapeHits", "EXCLUDE" if kitbash_only else "INCLUDE"),
                 required=False),
            Prop("query_params.ignore_landscape_hits", kitbash_only, required=False),
        ]
        if tags:
            props.insert(1, Prop("query_params.actor_tags_list", NameSet(tags)))
        self._add(NodeSpec(
            "surface", "World Ray Hit Query",
            [Variant(("PCGWorldRayHitSettings",), props)],
            summary=summary,
        ))

    def _sampler(self) -> None:
        layer = self.layer
        radius = max(layer.footprint_radius, 1.0)
        spec = self._add(NodeSpec(
            "sampler", "Surface Sampler",
            [Variant(("PCGSurfaceSamplerSettings",), [
                Prop("points_per_squared_meter", float(layer.density)),
                Prop("point_extents", Vec(radius, radius, radius)),
                Prop("looseness", 1.0, required=False),
                Prop("apply_density_to_points", True, required=False),
                Prop("unbounded", False, required=False),
                Prop("seed", int(layer.seed), required=False),
            ], main_in=SURFACE_PIN)],
            summary=f"{layer.density:g} points/m², footprint radius {radius:g} cm",
        ))
        self._edge(INPUT_NODE, INPUT_NODE_PINS, spec.key, BOUNDING_SHAPE_PIN)

    def _slope(self) -> None:
        low, high = effective_slope_range(self.layer)
        if not slope_is_limited(low, high):
            return
        lower, upper = slope_density_bounds(low, high)
        density = self._add(NodeSpec(
            "slope_density", "Normal To Density",
            [Variant(("PCGNormalToDensitySettings",), [
                Prop("normal", Vec(0.0, 0.0, 1.0)),
                Prop("offset", 0.0),
                Prop("strength", 1.0),
                Prop("density_mode", _enum("PCGNormalToDensityMode", "SET")),
            ])],
            summary="density = cos(slope)", feature="slope limits", optional=True,
        ))
        self._add(NodeSpec(
            "slope_filter", "Slope filter", density_range_variants(lower, upper),
            summary=f"keep slopes {low:g}°–{high:g}° (density {lower:.3f}–{upper:.3f})",
            feature="slope limits", optional=True, requires=(density.key,),
        ))

    def _height(self) -> None:
        layer = self.layer
        if not layer.use_height:
            return
        big = 1.0e7  # 100 km
        below = Box((-big, -big, -big), (big, big, float(layer.height_min)))
        above = Box((-big, -big, float(layer.height_max)), (big, big, big))
        boxes = self._add(NodeSpec(
            "height_limits", "Create Points (outside height range)",
            [Variant(("PCGCreatePointsSettings",), [
                Prop("points_to_create", PointBoxes((below, above))),
                Prop("coordinate_space", _enum("PCGCoordinateSpace", "WORLD")),
                Prop("cull_points_outside_volume", False, required=False),
            ])],
            feature="height range", optional=True,
        ), chain=False, helper=True)
        cut = self._add(NodeSpec(
            "height_filter", "Difference (height range)", difference_variants(),
            summary=f"keep {layer.height_min:g} to {layer.height_max:g} cm",
            feature="height range", optional=True, requires=(boxes.key,),
        ))
        self._edge(boxes.key, OUT, cut.key, DIFFERENCES_PIN)

    def _paint_layer(self) -> None:
        layer = self.layer
        name = layer.layer_name.strip()
        if not layer.use_layer_mask or not name or layer.surface is not SurfaceMode.LANDSCAPE_ONLY:
            return
        self._add(NodeSpec(
            "paint_layer", f"Paint layer '{name}'",
            [Variant(ATTRIBUTE_RANGE_CLASSES,
                     attribute_range_props(Selector(name), layer.layer_min_weight, 1000.0),
                     main_out=FILTER_INSIDE_PIN)],
            summary=f"keep weight >= {layer.layer_min_weight:g}",
            feature="paint-layer mask", optional=True,
        ))

    def _patches(self) -> None:
        layer = self.layer
        if not layer.use_patches:
            return
        size = max(layer.patch_size, 1.0)
        noise = self._add(NodeSpec(
            "patch_noise", "Spatial Noise (patches)",
            [Variant(("PCGSpatialNoiseSettings",), [
                Prop("mode", EnumValue(("PCGSpatialNoiseMode",), ("PERLIN2D", "PERLIN_2D", "PERLIN2_D", "PERLIN_2_D"))),
                Prop("transform", Xform(scale=Vec(size, size, size))),
                Prop("iterations", 3, required=False),
                Prop("value_target", Selector("$Density"), required=False),
                Prop("seed", int(layer.seed) + 7, required=False),
            ])],
            summary=f"Perlin noise, patches about {size / 100.0:g} m",
            feature="patches", optional=True,
        ))
        lower = clamp(1.0 - layer.patch_coverage, 0.0, 1.0)
        self._add(NodeSpec(
            "patch_filter", "Patch filter", density_range_variants(lower, 1.0 + 1e-6),
            summary=f"about {layer.patch_coverage * 100:.0f}% coverage",
            feature="patches", optional=True, requires=(noise.key,),
        ))

    def _keep_off_kitbash(self) -> None:
        layer, tag = self.layer, self.settings.kitbash_tag.strip()
        if layer.surface is not SurfaceMode.LANDSCAPE_ONLY or not layer.avoid_kitbash or not tag:
            return
        actors = self._add(NodeSpec(
            "kitbash_actors", f"Get Actor Data (tag '{tag}')",
            [Variant(("PCGDataFromActorSettings",), [
                Prop("actor_selector.actor_filter|actor_filter", _enum("PCGActorFilter", "ALL_WORLD_ACTORS")),
                Prop("actor_selector.actor_selection|actor_selection", _enum("PCGActorSelection", "BY_TAG")),
                Prop("actor_selector.actor_selection_tag|actor_selection_tag", NameValue(tag)),
                Prop("actor_selector.select_multiple|select_multiple", True),
                Prop("actor_selector.must_overlap_self", True, required=False),
                Prop("mode", _enum("PCGGetDataFromActorMode", "PARSE_ACTOR_COMPONENTS"), required=False),
            ])],
            feature="keep off kitbash meshes", optional=True,
        ), chain=False, helper=True)
        cut = self._add(NodeSpec(
            "keep_off_kitbash", "Difference (keep off kitbash)", difference_variants(),
            summary="removes points inside kitbash collision",
            feature="keep off kitbash meshes", optional=True, requires=(actors.key,),
        ))
        self._edge(actors.key, OUT, cut.key, DIFFERENCES_PIN)

    def _spacing(self) -> None:
        layer = self.layer
        if not layer.prune_overlaps:
            return
        self._add(NodeSpec(
            "spacing", "Self Pruning",
            [Variant(("PCGSelfPruningSettings",), [
                Prop("pruning_type", _enum("PCGSelfPruningType", "LARGE_TO_SMALL")),
                Prop("randomized_pruning", True, required=False),
                Prop("radius_similarity_factor", 0.25, required=False),
            ])],
            summary=f"no overlaps within {max(layer.footprint_radius, 1.0):g} cm",
            feature="minimum spacing", optional=True,
        ))

    def _avoid_earlier_layers(self) -> None:
        if not self.layer.avoid_previous_layers or not self.exclusion_sources:
            return
        cut = self._add(NodeSpec(
            "avoid_layers", "Difference (avoid layers above)", difference_variants(),
            summary=f"keeps clear of {len(self.exclusion_sources)} earlier layer(s)",
            feature="avoid layers above", optional=True,
        ))
        for source in self.exclusion_sources:
            self._edge(source, OUT, cut.key, DIFFERENCES_PIN)

    def _transform(self) -> None:
        layer = self.layer
        scale_min, scale_max = sorted((layer.scale_min, layer.scale_max))
        z_min, z_max = sorted((layer.z_offset_min, layer.z_offset_max))
        tilt = clamp(layer.max_tilt, 0.0, 90.0)
        yaw = 360.0 if layer.random_yaw else 0.0
        orientation = "aligned to surface" if layer.align_to_surface else "upright"
        self._add(NodeSpec(
            "transform", "Transform Points",
            [Variant(("PCGTransformPointsSettings",), [
                Prop("offset_min", Vec(0.0, 0.0, z_min)),
                Prop("offset_max", Vec(0.0, 0.0, z_max)),
                Prop("absolute_offset", False, required=False),
                Prop("rotation_min", Rot(pitch=-tilt, yaw=0.0, roll=-tilt)),
                Prop("rotation_max", Rot(pitch=tilt, yaw=yaw, roll=tilt)),
                Prop("absolute_rotation", not layer.align_to_surface),
                Prop("scale_min", Vec(scale_min, scale_min, scale_min)),
                Prop("scale_max", Vec(scale_max, scale_max, scale_max)),
                Prop("uniform_scale", True),
                Prop("absolute_scale", False, required=False),
                Prop("seed", int(layer.seed) + 13, required=False),
            ])],
            summary=f"scale {scale_min:g}–{scale_max:g}, {orientation}, tilt ±{tilt:g}°, "
                    f"z offset {z_min:g}–{z_max:g} cm",
            feature="scale, rotation and offset", optional=True,
        ))

    def _spawner(self) -> None:
        layer = self.layer
        meshes = [
            model_mesh_spec(entry, layer)
            for entry in layer.meshes
            if entry.path.strip() and entry.weight > 0
        ]
        self._add(NodeSpec(
            "spawner", "Static Mesh Spawner",
            [Variant(("PCGStaticMeshSpawnerSettings",), [Prop("seed", int(layer.seed) + 29, required=False)])],
            meshes=meshes,
            summary=", ".join(f"{m.path.rsplit('/', 1)[-1].split('.')[0]} x{m.weight}" for m in meshes),
        ))


def model_mesh_spec(entry: MeshEntry, layer: ScatterLayer) -> MeshSpec:
    return MeshSpec(
        path=entry.path.strip(),
        weight=int(entry.weight),
        cull_start=float(layer.cull_start),
        cull_end=float(layer.cull_end),
        collision=bool(layer.collision),
        cast_shadows=bool(layer.cast_shadows),
    )



# =============================================================================================
# Build report and the offline backend (window outside Unreal)
# =============================================================================================

INFO, WARNING, ERROR, DETAIL = "info", "warning", "error", "detail"


@dataclass(frozen=True)
class LandscapeInfo:
    id: str  # actor path name
    label: str


class BuildReport:
    """Collects what happened during a build instead of raising, so partial results are visible."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.lines.append((INFO, message))

    def warn(self, message: str) -> None:
        self.lines.append((WARNING, message))

    def error(self, message: str) -> None:
        self.lines.append((ERROR, message))

    def detail(self, message: str) -> None:
        """Only shown in the diagnostics output."""
        self.lines.append((DETAIL, message))

    @property
    def errors(self) -> List[str]:
        return [text for level, text in self.lines if level == ERROR]

    @property
    def warnings(self) -> List[str]:
        return [text for level, text in self.lines if level == WARNING]

    def visible_lines(self, verbose: bool = False) -> List[Tuple[str, str]]:
        return [(level, text) for level, text in self.lines if verbose or level != DETAIL]

    def text(self, verbose: bool = False) -> str:
        prefix = {INFO: "", WARNING: "WARNING: ", ERROR: "ERROR: ", DETAIL: "    "}
        return "\n".join(prefix[level] + text for level, text in self.visible_lines(verbose))

    def summary(self) -> str:
        if self.errors:
            return f"Finished with {len(self.errors)} error(s) and {len(self.warnings)} warning(s)."
        if self.warnings:
            return f"Done with {len(self.warnings)} warning(s)."
        return "Done."


class OfflineBackend:
    """Stand-in used when the window runs outside Unreal (preset editing only)."""

    available = False
    name = "Offline (outside Unreal)"

    def engine_version(self) -> str:
        return ""

    def list_landscapes(self) -> List[LandscapeInfo]:
        return []

    def landscape_bounds(self, landscape_id: str) -> Optional[Bounds]:
        return None

    def landscape_layer_names(self, landscape_id: str) -> List[str]:
        return []

    def selected_landscape_id(self) -> Optional[str]:
        return None

    def selected_static_mesh_paths(self) -> List[str]:
        return []

    def selected_actor_mesh_paths(self) -> List[str]:
        return []

    def tag_selected_actors(self, tag: str, add: bool = True) -> int:
        raise RuntimeError("Tagging actors only works inside the Unreal Editor.")

    def build(self, settings: ToolSettings, generate: Optional[bool] = None) -> BuildReport:
        raise RuntimeError("Building only works inside the Unreal Editor.")

    def cleanup(self, settings: ToolSettings) -> BuildReport:
        raise RuntimeError("Clean up only works inside the Unreal Editor.")

    def diagnostics(self) -> str:
        return "The PCG API check only runs inside the Unreal Editor."

    def session_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".pcg_scatter_tool", "last_session.json")



# =============================================================================================
# Unreal Editor backend
# =============================================================================================

# Marks graphs created by this tool; the tool refuses to wipe graphs without it.
TOOL_METADATA_KEY = "PCGScatterTool"
VOLUME_TAG = "PCGScatterTool"
VOLUME_GRAPH_TAG = "PCGScatterGraph="
LOG_PREFIX = "[PCG Scatter] "


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def _is_a(obj: Any, class_name: str) -> bool:
    cls = getattr(unreal, class_name, None)
    return cls is not None and obj is not None and isinstance(obj, cls)


def _path(obj: Any) -> str:
    try:
        return str(obj.get_path_name())
    except Exception:
        return ""


def _actor_bounds(actor: Any) -> Optional[Bounds]:
    try:
        origin, extent = actor.get_actor_bounds(False)
    except Exception:
        return None
    if max(abs(extent.x), abs(extent.y), abs(extent.z)) < 1e-3:
        return None
    return Bounds.from_origin_extent((origin.x, origin.y, origin.z), (extent.x, extent.y, extent.z))


def _tags(actor: Any) -> List[str]:
    try:
        return [str(tag) for tag in actor.get_editor_property("tags")]
    except Exception:
        return []


def _call_first(obj: Any, attempts: Sequence[Tuple[str, tuple]]) -> Optional[str]:
    """Calls the first method in `attempts` that exists and succeeds; returns its name."""
    for name, args in attempts:
        method = getattr(obj, name, None)
        if method is None:
            continue
        try:
            method(*args)
            return name
        except Exception:
            continue
    return None


def _pcg_component(volume: Any) -> Any:
    try:
        component = volume.get_component_by_class(unreal.PCGComponent)
        if component is not None:
            return component
    except Exception:
        pass
    try:
        return volume.get_editor_property("pcg_component")
    except Exception:
        return None


def _proxy_owner(proxy: Any) -> Any:
    method = getattr(proxy, "get_landscape_actor", None)
    if method is not None:
        try:
            return method()
        except Exception:
            pass
    for name in ("landscape_actor", "landscape_actor_ref"):
        try:
            owner = proxy.get_editor_property(name)
            if owner is not None:
                return owner
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------------------------
# Plan values -> Unreal values
# ---------------------------------------------------------------------------------------------

def resolve_enum(value: EnumValue) -> Any:
    for enum_name in value.enum_names:
        enum_type = getattr(unreal, enum_name, None)
        if enum_type is None:
            continue
        for member in value.members:
            found = getattr(enum_type, member, None)
            if found is not None:
                return found
    raise ValueError(f"enum value {'/'.join(value.enum_names)}.{'/'.join(value.members)} not found")


def _vector(values: Sequence[float]) -> Any:
    return unreal.Vector(float(values[0]), float(values[1]), float(values[2]))


def _box_point(box: Box) -> Any:
    point = unreal.PCGPoint()
    point.set_editor_property("transform", unreal.Transform())
    point.set_editor_property("bounds_min", _vector(box.min))
    point.set_editor_property("bounds_max", _vector(box.max))
    point.set_editor_property("density", 1.0)
    point.set_editor_property("steepness", 1.0)
    return point


def describe_value(value: Any) -> str:
    """Short text for plan values in the diagnostics output."""
    if isinstance(value, Vec):
        return f"({value.x:g}, {value.y:g}, {value.z:g})"
    if isinstance(value, Rot):
        return f"(pitch {value.pitch:g}, yaw {value.yaw:g}, roll {value.roll:g})"
    if isinstance(value, Xform):
        return f"scale ({value.scale.x:g}, {value.scale.y:g}, {value.scale.z:g})"
    if isinstance(value, EnumValue):
        return f"{value.enum_names[0]}.{'/'.join(value.members)}"
    if isinstance(value, (NameValue, Selector)):
        return value.text
    if isinstance(value, NameSet):
        return "{" + ", ".join(value.names) + "}"
    if isinstance(value, PointBoxes):
        return f"{len(value.boxes)} box point(s)"
    if isinstance(value, float):
        return f"{value:.6g}"
    return repr(value)


def unreal_value_candidates(value: Any) -> List[Any]:
    """Unreal representations to try, in order, for a plan value."""
    if isinstance(value, Vec):
        builders = [lambda: unreal.Vector(value.x, value.y, value.z)]
    elif isinstance(value, Rot):
        # Keyword arguments: unreal.Rotator's positional order is (roll, pitch, yaw).
        builders = [lambda: unreal.Rotator(roll=value.roll, pitch=value.pitch, yaw=value.yaw)]
    elif isinstance(value, Xform):
        builders = [lambda: unreal.Transform(
            location=unreal.Vector(value.location.x, value.location.y, value.location.z),
            rotation=unreal.Rotator(roll=value.rotation.roll, pitch=value.rotation.pitch, yaw=value.rotation.yaw),
            scale=unreal.Vector(value.scale.x, value.scale.y, value.scale.z),
        )]
    elif isinstance(value, EnumValue):
        builders = [lambda: resolve_enum(value)]
    elif isinstance(value, NameValue):
        builders = [lambda: unreal.Name(value.text), lambda: value.text]
    elif isinstance(value, NameSet):
        builders = [
            lambda: {unreal.Name(name) for name in value.names},
            lambda: [unreal.Name(name) for name in value.names],
            lambda: list(value.names),
        ]
    elif isinstance(value, PointBoxes):
        builders = [lambda: [_box_point(box) for box in value.boxes]]
    else:
        return [value]
    candidates: List[Any] = []
    last_error: Optional[Exception] = None
    for build in builders:
        try:
            candidates.append(build())
        except Exception as exc:
            last_error = exc
    if not candidates:
        raise ValueError(f"cannot convert {value!r}: {last_error}")
    return candidates


# ---------------------------------------------------------------------------------------------
# Attribute selectors (their fields are not editor properties, so they need special handling)
# ---------------------------------------------------------------------------------------------

def _selector_texts(text: str) -> List[str]:
    if text.startswith("$"):
        return [f"(Selection=PointProperty,PointProperty={text[1:]})", text]
    return [
        f'(Selection=Attribute,AttributeName="{text}")',
        f"(Selection=Attribute,AttributeName={text})",
        text,
    ]


def selector_matches(selector: Any, text: str) -> bool:
    try:
        exported = str(selector.export_text()).replace(" ", "")
    except Exception:
        return False
    if exported.strip('"') == text:
        return True
    if text.startswith("$"):
        return ("Selection=PointProperty" in exported
                and re.search(rf"PointProperty={re.escape(text[1:])}(?=[,)])", exported) is not None)
    named = re.search(rf'AttributeName="?{re.escape(text)}"?(?=[,)])', exported) is not None
    return named and "Selection=PointProperty" not in exported and "Selection=ExtraProperty" not in exported


def _selector_options(result: Any, fallback: Any, struct_type: type) -> Iterator[Any]:
    """Selector structs found in a helper call's result (a tuple, a struct, or in-place edits)."""
    items = list(result) if isinstance(result, tuple) else [result]
    items.append(fallback)
    for item in items:
        if not isinstance(item, unreal.StructBase):
            continue
        if isinstance(item, struct_type):
            yield item
            continue
        try:  # e.g. the base selector type was returned; copy it into the derived type
            converted = struct_type()
            converted.import_text(item.export_text())
            yield converted
        except Exception:
            continue


def apply_selector(current: Any, text: str) -> Optional[Any]:
    """Returns a copy of `current` pointed at `text` ('$Density' or an attribute name), or None."""
    struct_type = type(current)
    helpers = getattr(unreal, "PCGAttributePropertySelectorBlueprintHelpers", None)
    if helpers is not None:
        attempts: List[Tuple[str, Any]] = []
        if text.startswith("$"):
            point_properties = getattr(unreal, "PCGPointProperties", None)
            member = getattr(point_properties, text[1:].upper(), None) if point_properties else None
            if member is not None:
                attempts.append(("set_point_property", member))
        else:
            attempts.append(("set_attribute_name", text))
        attempts.append(("update", text))
        for function_name, argument in attempts:
            function = getattr(helpers, function_name, None)
            if function is None:
                continue
            candidate = struct_type()
            try:
                result = function(candidate, argument)
            except Exception:
                continue
            for option in _selector_options(result, candidate, struct_type):
                if selector_matches(option, text):
                    return option
    for text_form in _selector_texts(text):
        candidate = struct_type()
        try:
            candidate.import_text(text_form)
        except Exception:
            continue
        if selector_matches(candidate, text):
            return candidate
    return None


# ---------------------------------------------------------------------------------------------
# Editor properties by dotted path
# ---------------------------------------------------------------------------------------------

def _set_leaf(obj: Any, name: str, value: Any) -> None:
    if isinstance(value, Selector):
        updated = apply_selector(obj.get_editor_property(name), value.text)
        if updated is None:
            raise ValueError(f"could not point the selector at '{value.text}'")
        obj.set_editor_property(name, updated)
        return
    errors: List[str] = []
    for candidate in unreal_value_candidates(value):
        try:
            obj.set_editor_property(name, candidate)
            return
        except Exception as exc:
            errors.append(str(exc))
    raise ValueError(errors[-1] if errors else "no value to set")


def _set_nested(obj: Any, parts: List[str], value: Any) -> None:
    if len(parts) == 1:
        _set_leaf(obj, parts[0], value)
        return
    child = obj.get_editor_property(parts[0])
    if child is None:
        raise ValueError(f"'{parts[0]}' is empty")
    _set_nested(child, parts[1:], value)
    if isinstance(child, unreal.StructBase):  # structs come back as copies
        obj.set_editor_property(parts[0], child)


def set_property_path(obj: Any, path: str, value: Any) -> str:
    """Sets 'a.b.c' (nested structs) with '|'-separated alternatives; returns the one that worked."""
    errors: List[str] = []
    for alternative in path.split("|"):
        try:
            _set_nested(obj, alternative.split("."), value)
            return alternative
        except Exception as exc:
            errors.append(f"{alternative}: {exc}")
    raise ValueError("; ".join(errors))


# ---------------------------------------------------------------------------------------------
# Graph editing
# ---------------------------------------------------------------------------------------------

def _add_node(graph: Any, cls: Any) -> Tuple[Any, Any]:
    result = graph.add_node_of_type(cls)
    if isinstance(result, tuple):
        node = result[0] if result else None
        settings = result[1] if len(result) > 1 else None
    else:
        node, settings = result, None
    if node is not None and settings is None:
        for getter in ("get_settings", "get_settings_interface"):
            method = getattr(node, getter, None)
            if method is None:
                continue
            try:
                settings = method()
                break
            except Exception:
                continue
    return node, settings


def _remove_node(graph: Any, node: Any) -> None:
    try:
        graph.remove_node(node)
    except Exception:
        pass


def _apply_meshes(settings: Any, meshes: List[MeshSpec], report: BuildReport, title: str) -> bool:
    selector = None
    for name in ("mesh_selector_parameters", "mesh_selector_instance"):
        try:
            selector = settings.get_editor_property(name)
        except Exception:
            continue
        if selector is not None:
            break
    if selector is None:
        report.error(f"{title}: could not reach the spawner's mesh list.")
        return False
    entries = []
    for mesh in meshes:
        asset = unreal.load_asset(mesh.path)
        if asset is None:
            report.warn(f"{title}: static mesh not found: {mesh.path}")
            continue
        entry = unreal.PCGMeshSelectorWeightedEntry()
        if not _fill_mesh_entry(entry, asset, mesh):
            report.warn(f"{title}: could not add {mesh.path}")
            continue
        entries.append(entry)
    if not entries:
        report.error(f"{title}: none of the meshes could be added.")
        return False
    try:
        selector.set_editor_property("mesh_entries", entries)
    except Exception as exc:
        report.error(f"{title}: could not set the mesh list ({exc}).")
        return False
    return True


def _fill_mesh_entry(entry: Any, asset: Any, mesh: MeshSpec) -> bool:
    try:
        descriptor = entry.get_editor_property("descriptor")
    except Exception:
        descriptor = None
    if descriptor is None:  # engines before the ISM descriptor
        try:
            entry.set_editor_property("mesh", asset)
        except Exception:
            return False
    else:
        try:
            descriptor.set_editor_property("static_mesh", asset)
        except Exception:
            return False
        optional: List[Tuple[str, Any]] = [("cast_shadow", bool(mesh.cast_shadows))]
        if mesh.cull_end > 0:
            optional += [
                ("instance_start_cull_distance", int(mesh.cull_start)),
                ("instance_end_cull_distance", int(mesh.cull_end)),
            ]
        if not mesh.collision:
            optional += [
                ("body_instance.collision_enabled", EnumValue(("CollisionEnabled",), ("NO_COLLISION",))),
                ("body_instance.collision_profile_name", NameValue("NoCollision")),
            ]
        for path, value in optional:
            try:
                set_property_path(descriptor, path, value)
            except Exception:
                pass
        entry.set_editor_property("descriptor", descriptor)
    try:
        entry.set_editor_property("weight", max(1, int(mesh.weight)))
    except Exception:
        pass
    return True


class GraphExecutor:
    """Creates a plan's nodes and edges in a PCG graph, tolerating engine-version differences."""

    def __init__(self, graph: Any, report: BuildReport):
        self.graph = graph
        self.report = report
        self.nodes: Dict[str, Any] = {}
        self.variants: Dict[str, Variant] = {}
        self.specs: Dict[str, NodeSpec] = {}
        self.failed: Set[str] = set()
        self.bypassed: Set[str] = set()
        self.edges_made = 0
        self._pin_memory: Dict[Tuple[str, Tuple[str, ...]], str] = {}
        self._positions_ok = True

    def run(self, plan: GraphPlan) -> None:
        self.specs = {spec.key: spec for spec in plan.nodes}
        for spec in plan.nodes:
            self._create(spec)
        for edge in self._resolved_edges(plan):
            self._connect(edge)
        if not self._positions_ok:
            self.report.info("Node positions could not be set, so nodes may be stacked in the graph editor.")
        self.report.info(f"Graph has {len(self.nodes)} nodes and {self.edges_made} connections.")

    # -- nodes --------------------------------------------------------------------------------

    def _create(self, spec: NodeSpec) -> None:
        if any(key in self.failed or key in self.bypassed for key in spec.requires):
            self._give_up(spec, "a node it depends on is unavailable")
            return
        problem = "node type not found in this engine version"
        for variant in spec.variants:
            cls = next((getattr(unreal, n) for n in variant.classes if hasattr(unreal, n)), None)
            if cls is None:
                continue
            class_name = cls.__name__
            try:
                node, settings = _add_node(self.graph, cls)
            except Exception as exc:
                problem = f"could not add {class_name} ({exc})"
                continue
            if node is None or settings is None:
                if node is not None:
                    _remove_node(self.graph, node)
                problem = f"could not add {class_name}"
                continue
            self.report.detail(f"[{spec.layer}] {spec.title}: {class_name}")
            failures = self._apply_props(settings, variant.props, class_name)
            if spec.meshes is not None and not _apply_meshes(settings, spec.meshes, self.report, spec.title):
                failures.append("mesh list")
            if failures and spec.optional:
                _remove_node(self.graph, node)
                problem = f"{class_name} rejected {', '.join(failures)}"
                self.report.detail(f"    removed again: {problem}")
                continue
            for failure in failures:
                self.report.warn(f"[{spec.layer}] {spec.title}: could not set '{failure}' on {class_name}.")
            self._place(node, spec.pos)
            self.nodes[spec.key] = node
            self.variants[spec.key] = variant
            return
        self._give_up(spec, problem)

    def _apply_props(self, settings: Any, props: List[Prop], class_name: str) -> List[str]:
        failures: List[str] = []
        for prop in props:
            try:
                used = set_property_path(settings, prop.path, prop.value)
                self.report.detail(f"    {used} = {describe_value(prop.value)}")
            except Exception as exc:
                if prop.required:
                    failures.append(prop.path.split("|")[0])
                self.report.detail(f"    {'FAILED' if prop.required else 'skipped'}: {prop.path} ({exc})")
        return failures

    def _give_up(self, spec: NodeSpec, reason: str) -> None:
        if spec.optional:
            self.bypassed.add(spec.key)
            self.report.warn(
                f"[{spec.layer}] {spec.title} skipped ({reason}); {spec.feature or 'this step'} is not applied."
            )
        else:
            self.failed.add(spec.key)
            self.report.error(f"[{spec.layer}] {spec.title} could not be created ({reason}).")

    def _place(self, node: Any, pos: Tuple[int, int]) -> None:
        x, y = int(pos[0]), int(pos[1])
        if _call_first(node, [("set_node_position", (x, y))]) is not None:
            return
        try:
            node.set_editor_property("position_x", x)
            node.set_editor_property("position_y", y)
        except Exception:
            self._positions_ok = False

    # -- edges --------------------------------------------------------------------------------

    def _resolved_edges(self, plan: GraphPlan) -> List[EdgeSpec]:
        """Bridges skipped optional nodes and drops edges touching nodes that failed outright."""
        edges = list(plan.edges)
        for spec in plan.nodes:
            if spec.key not in self.bypassed:
                continue
            incoming = [e for e in edges if e.dst == spec.key and e.dst_pin == IN]
            outgoing = [e for e in edges if e.src == spec.key and e.src_pin == OUT]
            kept = [e for e in edges if e.dst != spec.key and e.src != spec.key]
            bridged = [EdgeSpec(i.src, i.src_pin, o.dst, o.dst_pin) for i in incoming for o in outgoing]
            edges = kept + bridged
        return [e for e in edges if e.src not in self.failed and e.dst not in self.failed]

    def _node(self, key: str) -> Any:
        if key == INPUT_NODE:
            try:
                return self.graph.get_input_node()
            except Exception:
                return None
        return self.nodes.get(key)

    def _pins(self, key: str, pin: PinRef) -> Tuple[str, ...]:
        if pin == IN:
            return self.variants[key].main_in
        if pin == OUT:
            return self.variants[key].main_out
        return tuple(pin)

    def _ordered(self, owner: Any, pins: Tuple[str, ...], direction: str) -> List[str]:
        remembered = self._pin_memory.get((f"{type(owner).__name__}:{direction}", pins))
        return [remembered] + [p for p in pins if p != remembered] if remembered else list(pins)

    def _title(self, key: str) -> str:
        if key == INPUT_NODE:
            return "Input"
        spec = self.specs.get(key)
        return f"[{spec.layer}] {spec.title}" if spec else key

    def _connect(self, edge: EdgeSpec) -> None:
        src, dst = self._node(edge.src), self._node(edge.dst)
        if src is None or dst is None:
            return
        try:
            src_pins, dst_pins = self._pins(edge.src, edge.src_pin), self._pins(edge.dst, edge.dst_pin)
        except KeyError:
            return
        for out_pin in self._ordered(src, src_pins, "out"):
            for in_pin in self._ordered(dst, dst_pins, "in"):
                try:
                    made = self.graph.add_edge(src, out_pin, dst, in_pin)
                except Exception:
                    made = None
                if made:
                    self._pin_memory[(f"{type(src).__name__}:out", src_pins)] = out_pin
                    self._pin_memory[(f"{type(dst).__name__}:in", dst_pins)] = in_pin
                    self.edges_made += 1
                    self.report.detail(f"{self._title(edge.src)}.{out_pin} -> {self._title(edge.dst)}.{in_pin}")
                    return
        self.report.warn(
            f"Could not connect {self._title(edge.src)} to {self._title(edge.dst)} "
            f"(tried output pins {list(src_pins)}, input pins {list(dst_pins)})."
        )


# ---------------------------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------------------------

class UnrealBackend:
    available = True
    name = "Unreal Editor"

    def __init__(self) -> None:
        self._actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)

    # -- general ------------------------------------------------------------------------------

    def engine_version(self) -> str:
        try:
            return str(unreal.SystemLibrary.get_engine_version())
        except Exception:
            return "unknown"

    def session_path(self) -> str:
        saved = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_saved_dir())
        return os.path.join(saved, "PCGScatterTool", "last_session.json")

    def _level_actors(self) -> List[Any]:
        return list(self._actor_subsystem.get_all_level_actors() or [])

    def _selected_actors(self) -> List[Any]:
        return list(self._actor_subsystem.get_selected_level_actors() or [])

    def _find_actor(self, path_name: str) -> Any:
        if not path_name:
            return None
        for actor in self._level_actors():
            if _path(actor) == path_name:
                return actor
        return None

    # -- landscapes ---------------------------------------------------------------------------

    def list_landscapes(self) -> List[LandscapeInfo]:
        actors = self._level_actors()
        mains = [a for a in actors if _is_a(a, "Landscape")] or [a for a in actors if _is_a(a, "LandscapeProxy")]
        return [LandscapeInfo(_path(a), str(a.get_actor_label())) for a in mains]

    def _landscape_parts(self, landscape: Any) -> List[Any]:
        """The landscape actor plus its streaming proxies (World Partition keeps components there)."""
        actors = self._level_actors()
        mains = [a for a in actors if _is_a(a, "Landscape")]
        proxies = [a for a in actors
                   if _is_a(a, "LandscapeProxy") and not _is_a(a, "Landscape") and _path(a) != _path(landscape)]
        if len(mains) <= 1:
            return [landscape] + proxies
        own = [landscape]
        for proxy in proxies:
            owner = _proxy_owner(proxy)
            if owner is not None and _path(owner) == _path(landscape):
                own.append(proxy)
        return own

    def landscape_bounds(self, landscape_id: str) -> Optional[Bounds]:
        landscape = self._find_actor(landscape_id)
        if landscape is None:
            return None
        bounds: Optional[Bounds] = None
        for part in self._landscape_parts(landscape):
            part_bounds = _actor_bounds(part)
            if part_bounds is not None:
                bounds = part_bounds if bounds is None else bounds.union(part_bounds)
        return bounds

    def landscape_layer_names(self, landscape_id: str) -> List[str]:
        landscape = self._find_actor(landscape_id)
        if landscape is None:
            return []
        names: Set[str] = set()
        try:
            for entry in landscape.get_editor_property("editor_layer_settings") or []:
                try:
                    info = entry.get_editor_property("layer_info_obj")
                    if info is not None:
                        names.add(str(info.get_editor_property("layer_name")))
                except Exception:
                    continue
        except Exception:
            pass
        try:  # newer engines keep target layers in a map on the landscape
            target_layers = landscape.get_editor_property("target_layers")
            names.update(str(key) for key in (target_layers.keys() if hasattr(target_layers, "keys") else []))
        except Exception:
            pass
        return sorted(name for name in names if name and name != "None")

    def selected_landscape_id(self) -> Optional[str]:
        for actor in self._selected_actors():
            if _is_a(actor, "Landscape"):
                return _path(actor)
            if _is_a(actor, "LandscapeProxy"):
                owner = _proxy_owner(actor)
                return _path(owner) if owner is not None else _path(actor)
        return None

    # -- content ------------------------------------------------------------------------------

    def selected_static_mesh_paths(self) -> List[str]:
        try:
            assets = unreal.EditorUtilityLibrary.get_selected_assets() or []
        except Exception:
            return []
        return [_path(asset) for asset in assets if isinstance(asset, unreal.StaticMesh)]

    def selected_actor_mesh_paths(self) -> List[str]:
        paths: List[str] = []
        for actor in self._selected_actors():
            try:
                components = actor.get_components_by_class(unreal.StaticMeshComponent) or []
            except Exception:
                continue
            for component in components:
                try:
                    mesh = component.get_editor_property("static_mesh")
                except Exception:
                    mesh = None
                if mesh is not None and _path(mesh) not in paths:
                    paths.append(_path(mesh))
        return paths

    def tag_selected_actors(self, tag: str, add: bool = True) -> int:
        tag = tag.strip()
        if not tag:
            raise ValueError("The tag is empty.")
        changed = 0
        with unreal.ScopedEditorTransaction(f"{'Add' if add else 'Remove'} tag {tag}"):
            for actor in self._selected_actors():
                tags = _tags(actor)
                if add and tag not in tags:
                    new_tags = tags + [tag]
                elif not add and tag in tags:
                    new_tags = [t for t in tags if t != tag]
                else:
                    continue
                actor.modify()
                actor.set_editor_property("tags", [unreal.Name(t) for t in new_tags])
                changed += 1
        return changed

    # -- build --------------------------------------------------------------------------------

    def build(self, settings: ToolSettings, generate: Optional[bool] = None) -> BuildReport:
        report = BuildReport()
        report.info(f"Unreal Engine {self.engine_version()}")
        for issue in validate_settings(settings):
            (report.error if issue.level == "error" else report.warn)(issue.message)
        if report.errors:
            return self._finish(report)
        if not hasattr(unreal, "PCGGraph"):
            report.error("PCG classes not found. Enable the 'Procedural Content Generation Framework' "
                         "plugin and restart the editor.")
            return self._finish(report)

        graph_plan = build_plan(settings)
        for note in graph_plan.notes:
            report.warn(note)
        if not graph_plan.nodes:
            report.error("Nothing to build: no layer is ready.")
            return self._finish(report)

        graph = self._load_or_create_graph(settings.graph_path, report)
        if graph is None:
            return self._finish(report)

        with unreal.ScopedSlowTask(3, "PCG Scatter Tool") as task:
            task.make_dialog(False)
            task.enter_progress_frame(1, "Clearing the graph")
            if not self._clear_graph(graph, report):
                return self._finish(report)
            task.enter_progress_frame(1, "Adding PCG nodes")
            GraphExecutor(graph, report).run(graph_plan)
            self._save(graph, report)
            task.enter_progress_frame(1, "Placing the PCG volume")
            volume = self._ensure_volume(settings, report)
            if volume is not None:
                do_generate = settings.generate_after_build if generate is None else generate
                self._assign_and_generate(volume, graph, settings, report, do_generate)
        report.info(f"Built {len(graph_plan.layers)} layer(s) into {settings.graph_path}.")
        return self._finish(report)

    def cleanup(self, settings: ToolSettings) -> BuildReport:
        report = BuildReport()
        volume = self._find_volume(settings.graph_path)
        if volume is None:
            report.warn("This tool has not placed a PCG volume for that graph yet.")
            return self._finish(report)
        component = _pcg_component(volume)
        if component is None or _call_first(component, [("cleanup_local", (True,)), ("cleanup", (True,))]) is None:
            report.error("Could not clean up the PCG volume.")
        else:
            report.info("Removed the generated meshes.")
        return self._finish(report)

    def _finish(self, report: BuildReport) -> BuildReport:
        for level, text in report.visible_lines():
            if level == "error":
                unreal.log_error(LOG_PREFIX + text)
            elif level == "warning":
                unreal.log_warning(LOG_PREFIX + text)
            else:
                unreal.log(LOG_PREFIX + text)
        return report

    def _load_or_create_graph(self, graph_path: str, report: BuildReport) -> Any:
        folder, name = split_asset_path(graph_path)
        asset_path = f"{folder}/{name}"
        library = unreal.EditorAssetLibrary
        if library.does_asset_exist(asset_path):
            graph = library.load_asset(asset_path)
            if graph is None or not isinstance(graph, unreal.PCGGraph):
                report.error(f"{asset_path} exists but is not a PCG graph. Pick another path.")
                return None
            if str(library.get_metadata_tag(graph, TOOL_METADATA_KEY) or "") != "1":
                report.error(f"{asset_path} is a PCG graph this tool did not create. The tool rebuilds its "
                             "graph from scratch, so pick another path to keep that graph safe.")
                return None
            report.info(f"Rebuilding {asset_path}.")
            return graph

        factories: List[Any] = []
        factory_class = getattr(unreal, "PCGGraphFactory", None)
        if factory_class is not None:
            try:
                factories.append(factory_class())
            except Exception:
                pass
        factories.append(None)
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        graph = None
        for factory in factories:
            try:
                graph = tools.create_asset(name, folder, unreal.PCGGraph, factory)
            except Exception as exc:
                report.detail(f"create_asset({factory}) failed: {exc}")
                graph = None
            if graph is not None:
                break
        if graph is None:
            report.error(f"Could not create a PCG graph at {asset_path}.")
            return None
        library.set_metadata_tag(graph, TOOL_METADATA_KEY, "1")
        report.info(f"Created {asset_path}.")
        return graph

    def _clear_graph(self, graph: Any, report: BuildReport) -> bool:
        keep = {_path(graph.get_input_node()), _path(graph.get_output_node())}
        graph_path = _path(graph)
        try:
            old_nodes = [node for node in unreal.ObjectIterator(unreal.PCGNode)
                         if _path(node.get_outer()) == graph_path and _path(node) not in keep]
        except Exception as exc:
            report.error(f"Could not list the graph's old nodes ({exc}). Delete the graph asset and build again.")
            return False
        for node in old_nodes:
            _remove_node(graph, node)
        report.detail(f"Removed {len(old_nodes)} old node(s).")
        return True

    def _save(self, graph: Any, report: BuildReport) -> None:
        try:
            unreal.EditorAssetLibrary.save_loaded_asset(graph, False)
        except Exception as exc:
            report.warn(f"The graph was built but not saved ({exc}). Save it from the Content Browser.")

    # -- volume -------------------------------------------------------------------------------

    def _find_volume(self, graph_path: str) -> Any:
        marker = VOLUME_GRAPH_TAG + graph_path
        for actor in self._level_actors():
            if _is_a(actor, "PCGVolume") and marker in _tags(actor):
                return actor
        return None

    def _ensure_volume(self, settings: ToolSettings, report: BuildReport) -> Any:
        volume = self._find_volume(settings.graph_path)
        if volume is None:
            with unreal.ScopedEditorTransaction("Create PCG scatter volume"):
                volume = self._actor_subsystem.spawn_actor_from_class(
                    unreal.PCGVolume, unreal.Vector(0.0, 0.0, 0.0), unreal.Rotator(0.0, 0.0, 0.0))
                if volume is None:
                    report.error("Could not spawn a PCG Volume.")
                    return None
                _, name = split_asset_path(settings.graph_path)
                volume.set_actor_label(f"{name}_Volume")
                volume.set_editor_property(
                    "tags", [unreal.Name(VOLUME_TAG), unreal.Name(VOLUME_GRAPH_TAG + settings.graph_path)])
            report.info(f"Placed PCG volume '{volume.get_actor_label()}'.")
        if settings.fit_volume and not self._fit_volume(volume, settings, report):
            return None
        return volume

    def _fit_volume(self, volume: Any, settings: ToolSettings, report: BuildReport) -> bool:
        bounds = self.landscape_bounds(settings.landscape)
        if bounds is None:
            report.error("Could not measure the landscape. In World Partition levels, load the landscape "
                         "region first (or size the volume by hand and turn off 'Fit PCG volume').")
            return False
        tag = settings.kitbash_tag.strip()
        if tag:  # make room for kitbash meshes that stick out above or below the landscape
            for actor in self._level_actors():
                if tag in _tags(actor):
                    actor_bounds = _actor_bounds(actor)
                    if actor_bounds is not None and actor_bounds.overlaps_xy(bounds):
                        bounds = Bounds(bounds.min[:2] + (min(bounds.min[2], actor_bounds.min[2]),),
                                        bounds.max[:2] + (max(bounds.max[2], actor_bounds.max[2]),))
        margin = max(float(settings.volume_margin), 0.0)
        target = (bounds.extent[0], bounds.extent[1], bounds.extent[2] + margin)
        with unreal.ScopedEditorTransaction("Fit PCG scatter volume"):
            volume.modify()
            volume.set_actor_rotation(unreal.Rotator(0.0, 0.0, 0.0), False)
            volume.set_actor_scale3d(unreal.Vector(1.0, 1.0, 1.0))
            base = _actor_bounds(volume)
            if base is None or min(base.extent) < 1.0:
                report.error("The PCG volume has no brush, so it cannot be sized. Resize it by hand and turn "
                             "off 'Fit PCG volume to landscape'.")
                return False
            volume.set_actor_location(unreal.Vector(*bounds.center), False, False)
            volume.set_actor_scale3d(unreal.Vector(*(target[i] / base.extent[i] for i in range(3))))
        report.info(f"Volume fitted to {bounds.size[0] / 100:.0f} x {bounds.size[1] / 100:.0f} m.")
        return True

    def _assign_and_generate(self, volume: Any, graph: Any, settings: ToolSettings,
                             report: BuildReport, generate: bool) -> None:
        component = _pcg_component(volume)
        if component is None:
            report.error("The PCG volume has no PCG component.")
            return
        try:
            component.set_graph(graph)
        except Exception as exc:
            report.error(f"Could not assign the graph to the volume ({exc}).")
            return
        if _call_first(component, [("set_is_partitioned", (bool(settings.partitioned),))]) is None:
            try:
                component.set_editor_property("is_partitioned", bool(settings.partitioned))
            except Exception:
                if settings.partitioned:
                    report.warn("Could not turn on partitioned generation; tick 'Is Partitioned' on the volume.")
        if not generate:
            report.info("Graph assigned to the volume. Press Generate on the volume when ready.")
            return
        _call_first(component, [("cleanup_local", (True,)), ("cleanup", (True,))])
        if _call_first(component, [("generate_local", (True,)), ("generate", (True,))]) is None:
            report.warn("Could not start generation; press Generate on the PCG volume.")
        else:
            report.info("Generation started. It runs in the background, so large landscapes take a moment.")

    # -- diagnostics --------------------------------------------------------------------------

    def diagnostics(self) -> str:
        report = BuildReport()
        report.info(f"Unreal Engine {self.engine_version()}, Python {sys.version.split()[0]}")
        report.info(f"Qt binding: {BINDING} (Qt {QT_VERSION})")
        if not hasattr(unreal, "PCGGraph"):
            report.error("PCG plugin not loaded.")
            return report.text(verbose=True)
        for name in ("PCGGraphFactory", "PCGVolume", "PCGComponent", "PCGPoint", "PCGMeshSelectorWeightedEntry",
                     "PCGAttributePropertySelectorBlueprintHelpers", "ObjectIterator"):
            report.info(f"{name}: {'found' if hasattr(unreal, name) else 'MISSING'}")
        check_plan = build_plan(diagnostic_settings())
        unresolved = sorted({spec.title for spec in check_plan.nodes
                             if not any(hasattr(unreal, name) for v in spec.variants for name in v.classes)})
        report.info("Node types with no class in this engine: " + (", ".join(unresolved) if unresolved else "none"))
        self._check_selectors(report)
        try:
            graph = unreal.new_object(unreal.PCGGraph)
        except Exception as exc:
            report.error(f"Could not create a scratch PCG graph: {exc}")
            return report.text(verbose=True)
        report.info("Building a test graph in memory (nothing is saved):")
        GraphExecutor(graph, report).run(check_plan)
        report.info(report.summary())
        return report.text(verbose=True)

    def _check_selectors(self, report: BuildReport) -> None:
        struct_type = getattr(unreal, "PCGAttributePropertyInputSelector", None)
        if struct_type is None:
            report.warn("PCGAttributePropertyInputSelector not found.")
            return
        for text in ("$Density", "Grass"):
            try:
                result = apply_selector(struct_type(), text)
            except Exception as exc:
                result, text = None, f"{text} ({exc})"
            report.info(f"Selector {text}: {'ok' if result is not None else 'FAILED'}")



# =============================================================================================
# Window
# =============================================================================================

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
        low, high = effective_slope_range(layer)
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

        samples = estimate_samples(layer, self._area_m2)
        if samples is None:
            self.estimate.setText("Pick a landscape to see how many points this makes.")
        elif samples > HEAVY_SAMPLE_COUNT:
            self.estimate.setText(f"About {samples:,} candidate points before masks. That's heavy: lower the "
                                  "density or turn on partitioned generation.")
        else:
            self.estimate.setText(f"About {samples:,} candidate points on this landscape before masks.")

        issues = validate_layer(layer, self._settings)
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
        self.settings = settings or self._load_session() or default_settings()
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
        broken = any(issue.level == "error" for issue in validate_layer(layer, self.settings))
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
        duplicate = copy_layer(self.settings.layers[row])
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
        text = build_plan(self.settings).describe()
        self.log("Graph preview", "head")
        self.log(text)
        return text

    def build(self, generate: bool) -> Optional[BuildReport]:
        self._read_target_fields()
        errors = [issue.message for issue in validate_settings(self.settings, self.backend.available)
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
            self.set_settings(load_settings(path))
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
            save_settings(self.settings, path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, TITLE, f"Could not save {path}:\n{exc}")
            return
        self.log(f"Saved preset {path}", "ok")

    def _load_session(self) -> Optional[ToolSettings]:
        try:
            path = self.backend.session_path()
            return load_settings(path) if os.path.isfile(path) else None
        except Exception:
            return None

    def save_session(self) -> None:
        try:
            path = self.backend.session_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            save_settings(self.settings, path)
        except Exception as exc:
            self.log(f"Could not save the session: {exc}", WARNING)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        self._read_target_fields()
        self.save_session()
        super().closeEvent(event)


def run_standalone(argv: Optional[List[str]] = None) -> int:
    """Opens the window outside Unreal (preset editing only)."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(list(argv or sys.argv))
    window = ScatterToolWindow(OfflineBackend())
    window.show()
    return exec_(app)



# =============================================================================================
# Launching
# =============================================================================================

def _state() -> Any:
    """Kept in sys.modules so that running the file again can close the window it opened before."""
    state = sys.modules.get("_pcg_scatter_tool_state")
    if state is None:
        state = types.ModuleType("_pcg_scatter_tool_state")
        state.window = None
        state.tick_handle = None
        sys.modules["_pcg_scatter_tool_state"] = state
    return state


def _qt_tick(app: Any) -> Callable[[float], None]:
    """Unreal owns the main loop, so Qt handles its events on every Slate tick."""
    busy = [False]

    def tick(_delta_seconds: float) -> None:
        if busy[0]:  # a Qt event started work that ticks Slate again; don't re-enter
            return
        busy[0] = True
        try:
            app.processEvents()
        finally:
            busy[0] = False

    return tick


def launch() -> Any:
    """Opens the tool window in the Unreal Editor, replacing one opened earlier."""
    if unreal is None:
        raise RuntimeError("launch() only works inside the Unreal Editor.")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["UnrealEditor"])
    app.setQuitOnLastWindowClosed(False)
    state = _state()
    if state.window is not None:
        try:
            state.window.close()
            state.window.deleteLater()
        except RuntimeError:  # already deleted on the C++ side
            pass
    state.window = ScatterToolWindow(UnrealBackend())
    state.window.show()
    try:
        unreal.parent_external_window_to_slate(int(state.window.winId()))
    except Exception:
        pass  # still usable, just not parented to the editor window
    if state.tick_handle is None:
        state.tick_handle = unreal.register_slate_post_tick_callback(_qt_tick(app))
    return state.window


def main() -> None:
    if unreal is not None:
        launch()
    else:
        sys.exit(run_standalone())


if __name__ == "__main__":
    main()
