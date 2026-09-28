"""Unreal Editor side of the PCG Scatter Tool (written for UE 5.6).

Everything that touches the `unreal` module lives here. PCG's Python surface shifts between
engine versions, so every call is defensive: nodes come with fallbacks (see plan.py), failures are
collected in a BuildReport instead of raising, and optional steps are skipped with a warning
rather than wired into the graph half-configured.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import unreal

from . import model
from . import plan as planlib
from .backends import BuildReport, LandscapeInfo
from .model import Bounds, ToolSettings

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

def resolve_enum(value: planlib.EnumValue) -> Any:
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


def _box_point(box: planlib.Box) -> Any:
    point = unreal.PCGPoint()
    point.set_editor_property("transform", unreal.Transform())
    point.set_editor_property("bounds_min", _vector(box.min))
    point.set_editor_property("bounds_max", _vector(box.max))
    point.set_editor_property("density", 1.0)
    point.set_editor_property("steepness", 1.0)
    return point


def describe_value(value: Any) -> str:
    """Short text for plan values in the diagnostics output."""
    P = planlib
    if isinstance(value, P.Vec):
        return f"({value.x:g}, {value.y:g}, {value.z:g})"
    if isinstance(value, P.Rot):
        return f"(pitch {value.pitch:g}, yaw {value.yaw:g}, roll {value.roll:g})"
    if isinstance(value, P.Xform):
        return f"scale ({value.scale.x:g}, {value.scale.y:g}, {value.scale.z:g})"
    if isinstance(value, P.EnumValue):
        return f"{value.enum_names[0]}.{'/'.join(value.members)}"
    if isinstance(value, (P.NameValue, P.Selector)):
        return value.text
    if isinstance(value, P.NameSet):
        return "{" + ", ".join(value.names) + "}"
    if isinstance(value, P.PointBoxes):
        return f"{len(value.boxes)} box point(s)"
    if isinstance(value, float):
        return f"{value:.6g}"
    return repr(value)


def unreal_value_candidates(value: Any) -> List[Any]:
    """Unreal representations to try, in order, for a plan value."""
    P = planlib
    if isinstance(value, P.Vec):
        builders = [lambda: unreal.Vector(value.x, value.y, value.z)]
    elif isinstance(value, P.Rot):
        # Keyword arguments: unreal.Rotator's positional order is (roll, pitch, yaw).
        builders = [lambda: unreal.Rotator(roll=value.roll, pitch=value.pitch, yaw=value.yaw)]
    elif isinstance(value, P.Xform):
        builders = [lambda: unreal.Transform(
            location=unreal.Vector(value.location.x, value.location.y, value.location.z),
            rotation=unreal.Rotator(roll=value.rotation.roll, pitch=value.rotation.pitch, yaw=value.rotation.yaw),
            scale=unreal.Vector(value.scale.x, value.scale.y, value.scale.z),
        )]
    elif isinstance(value, P.EnumValue):
        builders = [lambda: resolve_enum(value)]
    elif isinstance(value, P.NameValue):
        builders = [lambda: unreal.Name(value.text), lambda: value.text]
    elif isinstance(value, P.NameSet):
        builders = [
            lambda: {unreal.Name(name) for name in value.names},
            lambda: [unreal.Name(name) for name in value.names],
            lambda: list(value.names),
        ]
    elif isinstance(value, P.PointBoxes):
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
    if isinstance(value, planlib.Selector):
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


def _apply_meshes(settings: Any, meshes: List[planlib.MeshSpec], report: BuildReport, title: str) -> bool:
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


def _fill_mesh_entry(entry: Any, asset: Any, mesh: planlib.MeshSpec) -> bool:
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
                ("body_instance.collision_enabled", planlib.EnumValue(("CollisionEnabled",), ("NO_COLLISION",))),
                ("body_instance.collision_profile_name", planlib.NameValue("NoCollision")),
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
        self.variants: Dict[str, planlib.Variant] = {}
        self.specs: Dict[str, planlib.NodeSpec] = {}
        self.failed: Set[str] = set()
        self.bypassed: Set[str] = set()
        self.edges_made = 0
        self._pin_memory: Dict[Tuple[str, Tuple[str, ...]], str] = {}
        self._positions_ok = True

    def run(self, plan: planlib.GraphPlan) -> None:
        self.specs = {spec.key: spec for spec in plan.nodes}
        for spec in plan.nodes:
            self._create(spec)
        for edge in self._resolved_edges(plan):
            self._connect(edge)
        if not self._positions_ok:
            self.report.info("Node positions could not be set, so nodes may be stacked in the graph editor.")
        self.report.info(f"Graph has {len(self.nodes)} nodes and {self.edges_made} connections.")

    # -- nodes --------------------------------------------------------------------------------

    def _create(self, spec: planlib.NodeSpec) -> None:
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

    def _apply_props(self, settings: Any, props: List[planlib.Prop], class_name: str) -> List[str]:
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

    def _give_up(self, spec: planlib.NodeSpec, reason: str) -> None:
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

    def _resolved_edges(self, plan: planlib.GraphPlan) -> List[planlib.EdgeSpec]:
        """Bridges skipped optional nodes and drops edges touching nodes that failed outright."""
        edges = list(plan.edges)
        for spec in plan.nodes:
            if spec.key not in self.bypassed:
                continue
            incoming = [e for e in edges if e.dst == spec.key and e.dst_pin == planlib.IN]
            outgoing = [e for e in edges if e.src == spec.key and e.src_pin == planlib.OUT]
            kept = [e for e in edges if e.dst != spec.key and e.src != spec.key]
            bridged = [planlib.EdgeSpec(i.src, i.src_pin, o.dst, o.dst_pin) for i in incoming for o in outgoing]
            edges = kept + bridged
        return [e for e in edges if e.src not in self.failed and e.dst not in self.failed]

    def _node(self, key: str) -> Any:
        if key == planlib.INPUT_NODE:
            try:
                return self.graph.get_input_node()
            except Exception:
                return None
        return self.nodes.get(key)

    def _pins(self, key: str, pin: planlib.PinRef) -> Tuple[str, ...]:
        if pin == planlib.IN:
            return self.variants[key].main_in
        if pin == planlib.OUT:
            return self.variants[key].main_out
        return tuple(pin)

    def _ordered(self, owner: Any, pins: Tuple[str, ...], direction: str) -> List[str]:
        remembered = self._pin_memory.get((f"{type(owner).__name__}:{direction}", pins))
        return [remembered] + [p for p in pins if p != remembered] if remembered else list(pins)

    def _title(self, key: str) -> str:
        if key == planlib.INPUT_NODE:
            return "Input"
        spec = self.specs.get(key)
        return f"[{spec.layer}] {spec.title}" if spec else key

    def _connect(self, edge: planlib.EdgeSpec) -> None:
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
        for issue in model.validate_settings(settings):
            (report.error if issue.level == "error" else report.warn)(issue.message)
        if report.errors:
            return self._finish(report)
        if not hasattr(unreal, "PCGGraph"):
            report.error("PCG classes not found. Enable the 'Procedural Content Generation Framework' "
                         "plugin and restart the editor.")
            return self._finish(report)

        graph_plan = planlib.build_plan(settings)
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
        folder, name = model.split_asset_path(graph_path)
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
                _, name = model.split_asset_path(settings.graph_path)
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
        try:
            from . import qt_compat
            report.info(f"Qt binding: {qt_compat.BINDING} (Qt {qt_compat.QT_VERSION})")
        except Exception as exc:
            report.warn(f"Qt binding not available: {exc}")
        if not hasattr(unreal, "PCGGraph"):
            report.error("PCG plugin not loaded.")
            return report.text(verbose=True)
        for name in ("PCGGraphFactory", "PCGVolume", "PCGComponent", "PCGPoint", "PCGMeshSelectorWeightedEntry",
                     "PCGAttributePropertySelectorBlueprintHelpers", "ObjectIterator"):
            report.info(f"{name}: {'found' if hasattr(unreal, name) else 'MISSING'}")
        check_plan = planlib.build_plan(model.diagnostic_settings())
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
