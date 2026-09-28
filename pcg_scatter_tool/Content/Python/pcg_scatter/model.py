"""Settings, scatter layers, validation and presets for the PCG Scatter Tool.

Pure Python (no Unreal or Qt imports), so the UI, the graph planner and the tests share it.
Units follow Unreal: distances in centimetres, angles in degrees, density in points per m².
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

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
