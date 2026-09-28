"""Turns ToolSettings into an engine-agnostic description of the PCG graph (a GraphPlan).

The Unreal backend executes the plan; the UI preview and the tests read it directly. Because PCG
node classes, properties and pin names move between engine versions, a node lists one or more
Variants (class candidates plus the properties to set), and edges refer to a node's main input
or output instead of hard-coding pin labels. Optional nodes can be skipped by the executor, which
then connects their neighbours directly.

Per layer the graph is a chain:

    surface (landscape data or downward ray hits) -> Surface Sampler -> slope filter ->
    height range -> paint layer -> patch noise -> keep off kitbash -> spacing ->
    avoid earlier layers -> Transform Points -> Static Mesh Spawner
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, Union

from . import model
from .model import ScatterLayer, SurfaceMode, ToolSettings


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
        errors = [issue for issue in model.validate_layer(layer, settings) if issue.level == "error"]
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
        low, high = model.effective_slope_range(self.layer)
        if not model.slope_is_limited(low, high):
            return
        lower, upper = model.slope_density_bounds(low, high)
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
        lower = model.clamp(1.0 - layer.patch_coverage, 0.0, 1.0)
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
        tilt = model.clamp(layer.max_tilt, 0.0, 90.0)
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


def model_mesh_spec(entry: model.MeshEntry, layer: ScatterLayer) -> MeshSpec:
    return MeshSpec(
        path=entry.path.strip(),
        weight=int(entry.weight),
        cull_start=float(layer.cull_start),
        cull_end=float(layer.cull_end),
        collision=bool(layer.collision),
        cast_shadows=bool(layer.cast_shadows),
    )
