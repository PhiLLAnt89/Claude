"""A small stand-in for the parts of Unreal's `unreal` module that pcg_scatter uses.

It models PCG graphs loosely (nodes, pins, edges, editor properties) so the backend's logic can run
outside the editor. It is not a faithful copy of Unreal: property names, pins and behaviour follow
what the tool expects from UE 5.6, and the real engine still has to be checked in the editor (the
tool's "Check PCG API" button does that).
"""
from __future__ import annotations

import copy
import itertools
import re

DEFAULT_OPTIONS = {
    "input_node_pins": ("In",),  # output pins of a graph's Input node
    "selector_import_fails": False,  # make StructBase.import_text fail for selectors
    "selector_helpers": False,  # expose PCGAttributePropertySelectorBlueprintHelpers
    "saved_dir": "/tmp/fake_unreal_saved",
}
OPTIONS = dict(DEFAULT_OPTIONS)

_registry = []  # every Object ever created, for ObjectIterator
_assets = {}  # "/Game/Folder/Name" -> object
_metadata = {}  # (id(object), tag) -> value
_subsystems = {}
_selected_assets = []
_counter = itertools.count()
logs = []
tick_callbacks = []


def reset(**options):
    for store in (_registry, _selected_assets, logs, tick_callbacks):
        store.clear()
    for store in (_assets, _metadata, _subsystems):
        store.clear()
    OPTIONS.clear()
    OPTIONS.update(DEFAULT_OPTIONS)
    OPTIONS.update(options)
    register_static_mesh("/Engine/BasicShapes/Cube.Cube")  # engine content always exists


class Name(str):
    pass


def _enum(name, *members):
    return type(name, (), {member: f"{name}.{member}" for member in members})


PCGNormalToDensityMode = _enum("PCGNormalToDensityMode", "SET", "MINIMUM", "MAXIMUM", "ADD", "SUBTRACT", "MULTIPLY")
PCGWorldQueryFilterByTag = _enum("PCGWorldQueryFilterByTag", "NO_TAG_FILTER", "INCLUDE_TAGGED", "EXCLUDE_TAGGED")
PCGWorldQuerySelectLandscapeHits = _enum("PCGWorldQuerySelectLandscapeHits", "EXCLUDE", "INCLUDE", "REQUIRE")
PCGDifferenceDensityFunction = _enum("PCGDifferenceDensityFunction", "MINIMUM", "CLAMPED_SUBTRACTION", "BINARY")
PCGDifferenceMode = _enum("PCGDifferenceMode", "INFERRED", "CONTINUOUS", "DISCRETE")
PCGSelfPruningType = _enum("PCGSelfPruningType", "LARGE_TO_SMALL", "SMALL_TO_LARGE", "ALL_EQUAL", "NONE")
PCGCoordinateSpace = _enum("PCGCoordinateSpace", "WORLD", "ORIGINAL_COMPONENT", "LOCAL_COMPONENT")
PCGActorFilter = _enum("PCGActorFilter", "SELF", "PARENT", "ROOT", "ALL_WORLD_ACTORS", "ORIGINAL")
PCGActorSelection = _enum("PCGActorSelection", "BY_TAG", "BY_CLASS", "UNKNOWN")
PCGGetDataFromActorMode = _enum("PCGGetDataFromActorMode", "PARSE_ACTOR_COMPONENTS", "GET_SINGLE_POINT")
PCGSpatialNoiseMode = _enum("PCGSpatialNoiseMode", "PERLIN2D", "CAUSTIC2D", "VORONOI2D", "EDGE_MASK2D")
PCGMetadataTypes = _enum("PCGMetadataTypes", "FLOAT", "DOUBLE", "INTEGER32", "INTEGER64", "STRING", "NAME")
PCGPointProperties = _enum("PCGPointProperties", "DENSITY", "POSITION", "ROTATION", "SCALE", "STEEPNESS")
CollisionEnabled = _enum("CollisionEnabled", "NO_COLLISION", "QUERY_ONLY", "PHYSICS_ONLY", "QUERY_AND_PHYSICS")
AppMsgType = _enum("AppMsgType", "OK")


# ---------------------------------------------------------------------------------------------
# Editor properties
# ---------------------------------------------------------------------------------------------

class _Properties:
    _props = {}

    def _init_props(self):
        self._values = {}
        for klass in reversed(type(self).__mro__):
            for key, default in klass.__dict__.get("_props", {}).items():
                self._values[key] = copy.deepcopy(default)

    def get_editor_property(self, name):
        if name not in self._values:
            raise AttributeError(f"{type(self).__name__}: no editor property '{name}'")
        value = self._values[name]
        return copy.deepcopy(value) if isinstance(value, StructBase) else value

    def set_editor_property(self, name, value, notify_mode=None):
        if name not in self._values:
            raise AttributeError(f"{type(self).__name__}: no editor property '{name}'")
        current = self._values[name]
        # Unreal is strict about these, so the fake is too.
        if isinstance(current, StructBase) and not isinstance(value, type(current)):
            raise TypeError(f"{name} expects {type(current).__name__}, got {type(value).__name__}")
        if isinstance(current, bool) and not isinstance(value, bool):
            raise TypeError(f"{name} expects bool, got {type(value).__name__}")
        if isinstance(current, int) and not isinstance(current, bool) and isinstance(value, float):
            raise TypeError(f"{name} expects int, got float")
        self._values[name] = copy.deepcopy(value) if isinstance(value, StructBase) else value


class StructBase(_Properties):
    def __init__(self, **kwargs):
        self._init_props()
        for key, value in kwargs.items():
            self.set_editor_property(key, value)

    def export_text(self):
        return "(" + ",".join(f"{key}={value}" for key, value in self._values.items()) + ")"

    def import_text(self, text):
        raise NotImplementedError


class Vector(StructBase):
    _props = {"x": 0.0, "y": 0.0, "z": 0.0}

    def __init__(self, x=0.0, y=0.0, z=0.0):
        super().__init__()
        self._values.update(x=float(x), y=float(y), z=float(z))

    x = property(lambda self: self._values["x"])
    y = property(lambda self: self._values["y"])
    z = property(lambda self: self._values["z"])


class Rotator(StructBase):
    _props = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}

    def __init__(self, roll=0.0, pitch=0.0, yaw=0.0):  # Unreal's positional order
        super().__init__()
        self._values.update(roll=float(roll), pitch=float(pitch), yaw=float(yaw))


class Transform(StructBase):
    _props = {"translation": Vector(), "rotation": Rotator(), "scale3d": Vector(1, 1, 1)}

    def __init__(self, location=None, rotation=None, scale=None):
        super().__init__()
        if location is not None:
            self._values["translation"] = location
        if rotation is not None:
            self._values["rotation"] = rotation
        if scale is not None:
            self._values["scale3d"] = scale


class PCGAttributePropertySelector(StructBase):
    """Real selector fields are not editor properties; only text import/export reaches them."""

    def __init__(self):
        super().__init__()
        self._selection = {"Selection": "Attribute", "AttributeName": '"@Last"', "PointProperty": "Density"}

    def export_text(self):
        return "(" + ",".join(f"{key}={value}" for key, value in self._selection.items()) + ")"

    def import_text(self, text):
        if OPTIONS["selector_import_fails"]:
            raise RuntimeError("import_text not supported")
        text = text.strip()
        if not text.startswith("("):
            raise ValueError("expected a (...) struct")
        for key, value in re.findall(r'(\w+)=("[^"]*"|[^,)]*)', text):
            if key == "AttributeName" and not value.startswith('"'):
                value = f'"{value}"'
            self._selection[key] = value


class PCGAttributePropertyInputSelector(PCGAttributePropertySelector):
    pass


class PCGAttributePropertyOutputSelector(PCGAttributePropertySelector):
    pass


class _SelectorHelpers:
    """Mimics Blueprint helpers with by-ref struct parameters: Python gets (return value, struct)."""

    @staticmethod
    def set_attribute_name(selector, name):
        result = copy.deepcopy(selector)
        result._selection.update(Selection="Attribute", AttributeName=f'"{name}"')
        return True, result

    @staticmethod
    def set_point_property(selector, point_property):
        result = copy.deepcopy(selector)
        result._selection.update(Selection="PointProperty", PointProperty=point_property.split(".")[-1].title())
        return True, result


def __getattr__(name):  # module-level: optional classes controlled by OPTIONS
    if name == "PCGAttributePropertySelectorBlueprintHelpers" and OPTIONS["selector_helpers"]:
        return _SelectorHelpers
    raise AttributeError(name)


class PCGLandscapeDataProps(StructBase):
    _props = {"get_height_only": False, "get_layer_weights": True}


class PCGWorldRayHitQueryParams(StructBase):
    _props = {
        "override_default_params": False, "trace_complex": False, "ignore_pcg_hits": True,
        "ignore_self_hits": True, "actor_tag_filter": PCGWorldQueryFilterByTag.NO_TAG_FILTER,
        "actor_tags_list": set(), "select_landscape_hits": PCGWorldQuerySelectLandscapeHits.INCLUDE,
        "apply_metadata_from_landscape": False,
    }


class PCGActorSelectorSettings(StructBase):
    _props = {
        "actor_filter": PCGActorFilter.SELF, "actor_selection": PCGActorSelection.BY_TAG,
        "actor_selection_tag": Name(""), "select_multiple": False, "must_overlap_self": False,
    }


class PCGMetadataTypesConstantStruct(StructBase):
    _props = {"type": PCGMetadataTypes.DOUBLE, "double_value": 0.0, "float_value": 0.0}


class PCGAttributeFilterThresholdSettings(StructBase):
    _props = {
        "inclusive": False, "use_constant_threshold": False,
        "threshold_attribute": PCGAttributePropertyInputSelector(),
        "attribute_types": PCGMetadataTypesConstantStruct(),
    }


class PCGPoint(StructBase):
    _props = {
        "transform": Transform(), "density": 1.0, "bounds_min": Vector(-1, -1, -1),
        "bounds_max": Vector(1, 1, 1), "steepness": 0.5, "seed": 0,
    }


class BodyInstance(StructBase):
    _props = {"collision_enabled": CollisionEnabled.QUERY_AND_PHYSICS, "collision_profile_name": Name("Default")}


class SoftISMComponentDescriptor(StructBase):
    _props = {
        "static_mesh": None, "instance_start_cull_distance": 0, "instance_end_cull_distance": 0,
        "cast_shadow": True, "body_instance": BodyInstance(),
    }


class PCGMeshSelectorWeightedEntry(StructBase):
    _props = {"descriptor": SoftISMComponentDescriptor(), "weight": 1}


# ---------------------------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------------------------

class Object(_Properties):
    def __init__(self, outer=None, name=None):
        self._init_props()
        self._outer = outer
        self._name = name or f"{type(self).__name__}_{next(_counter)}"
        self._asset_path = None
        _registry.append(self)

    def __deepcopy__(self, memo):  # UObjects are references
        return self

    def get_outer(self):
        return self._outer

    def get_name(self):
        return self._name

    def get_path_name(self):
        if self._asset_path:
            return self._asset_path
        if self._outer is None:
            return f"/Temp/{self._name}"
        return f"{self._outer.get_path_name()}:{self._name}"

    def modify(self, always_mark_dirty=True):
        return True


def ObjectIterator(cls=None):  # noqa: N802 (matches Unreal)
    return iter([obj for obj in list(_registry) if cls is None or isinstance(obj, cls)])


def new_object(cls, outer=None, name=None):
    return cls(outer=outer, name=name)


class StaticMesh(Object):
    pass


class PCGSettings(Object):
    _props = {"seed": 42}
    in_pins = ("In",)
    out_pins = ("Out",)


class PCGGraphInputOutputSettings(PCGSettings):
    in_pins = ()
    out_pins = ()


class PCGGetLandscapeSettings(PCGSettings):
    _props = {"sampling_properties": PCGLandscapeDataProps()}
    in_pins = ()


class PCGWorldRayHitSettings(PCGSettings):
    _props = {"query_params": PCGWorldRayHitQueryParams()}
    in_pins = ()


class PCGSurfaceSamplerSettings(PCGSettings):
    _props = {
        "points_per_squared_meter": 0.1, "point_extents": Vector(100, 100, 100), "looseness": 1.0,
        "apply_density_to_points": True, "point_steepness": 0.5, "unbounded": False,
    }
    in_pins = ("Surface", "Bounding Shape")


class PCGNormalToDensitySettings(PCGSettings):
    _props = {"normal": Vector(0, 0, 1), "offset": 0.0, "strength": 1.0, "density_mode": PCGNormalToDensityMode.SET}


class PCGDensityFilterSettings(PCGSettings):
    _props = {"lower_bound": 0.5, "upper_bound": 1.0, "invert_filter": False}


class PCGAttributeFilteringRangeSettings(PCGSettings):
    _props = {
        "target_attribute": PCGAttributePropertyInputSelector(),
        "min_threshold": PCGAttributeFilterThresholdSettings(),
        "max_threshold": PCGAttributeFilterThresholdSettings(),
    }
    out_pins = ("InsideFilter", "OutsideFilter")


class PCGCreatePointsSettings(PCGSettings):
    _props = {"points_to_create": [], "coordinate_space": PCGCoordinateSpace.LOCAL_COMPONENT,
              "cull_points_outside_volume": False}
    in_pins = ()


class PCGDifferenceSettings(PCGSettings):
    _props = {"density_function": PCGDifferenceDensityFunction.MINIMUM, "mode": PCGDifferenceMode.INFERRED}
    in_pins = ("Source", "Differences")


class PCGDataFromActorSettings(PCGSettings):
    _props = {"actor_selector": PCGActorSelectorSettings(), "mode": PCGGetDataFromActorMode.PARSE_ACTOR_COMPONENTS}
    in_pins = ()


class PCGSelfPruningSettings(PCGSettings):
    _props = {"pruning_type": PCGSelfPruningType.LARGE_TO_SMALL, "radius_similarity_factor": 0.25,
              "randomized_pruning": True}


class PCGSpatialNoiseSettings(PCGSettings):
    _props = {"mode": PCGSpatialNoiseMode.PERLIN2D, "transform": Transform(), "iterations": 4,
              "value_target": PCGAttributePropertyOutputSelector()}


class PCGTransformPointsSettings(PCGSettings):
    _props = {
        "offset_min": Vector(), "offset_max": Vector(), "absolute_offset": False,
        "rotation_min": Rotator(), "rotation_max": Rotator(), "absolute_rotation": False,
        "scale_min": Vector(1, 1, 1), "scale_max": Vector(1, 1, 1), "absolute_scale": False,
        "uniform_scale": True,
    }


class PCGMeshSelectorWeighted(Object):
    _props = {"mesh_entries": []}


class PCGStaticMeshSpawnerSettings(PCGSettings):
    def __init__(self, outer=None, name=None):
        super().__init__(outer, name)
        self._values["mesh_selector_parameters"] = PCGMeshSelectorWeighted(outer=self)


class PCGNode(Object):
    def __init__(self, outer=None, name=None, settings=None, in_pins=(), out_pins=()):
        super().__init__(outer, name)
        self._settings = settings
        self.in_pins = tuple(in_pins)
        self.out_pins = tuple(out_pins)
        self.position = None

    def get_settings(self):
        return self._settings

    def set_node_position(self, x, y):
        self.position = (x, y)


class PCGGraph(Object):
    def __init__(self, outer=None, name=None):
        super().__init__(outer, name)
        self._nodes = []
        self.edges = []  # (from_node, from_pin, to_node, to_pin)
        self.failed_edge_attempts = 0
        self._input = PCGNode(self, "DefaultInputNode", PCGGraphInputOutputSettings(),
                              in_pins=(), out_pins=OPTIONS["input_node_pins"])
        self._output = PCGNode(self, "DefaultOutputNode", PCGGraphInputOutputSettings(), in_pins=("In",))

    def add_node_of_type(self, settings_class):
        node = PCGNode(self)
        settings = settings_class(outer=node)
        node._settings = settings
        node.in_pins, node.out_pins = settings_class.in_pins, settings_class.out_pins
        self._nodes.append(node)
        return node, settings

    def add_edge(self, from_node, from_pin_label, to_node, to_pin_label):
        if from_pin_label not in from_node.out_pins or to_pin_label not in to_node.in_pins:
            self.failed_edge_attempts += 1
            return None
        self.edges.append((from_node, str(from_pin_label), to_node, str(to_pin_label)))
        return to_node

    def remove_node(self, node):
        if node in self._nodes:
            self._nodes.remove(node)
        self.edges = [e for e in self.edges if e[0] is not node and e[2] is not node]

    def get_input_node(self):
        return self._input

    def get_output_node(self):
        return self._output

    # test helpers
    def nodes(self):
        return list(self._nodes)

    def nodes_of(self, settings_class):
        return [n for n in self._nodes if isinstance(n.get_settings(), settings_class)]

    def incoming(self, node):
        return [e for e in self.edges if e[2] is node]

    def outgoing(self, node):
        return [e for e in self.edges if e[0] is node]


class PCGGraphFactory(Object):
    pass


# ---------------------------------------------------------------------------------------------
# Actors and the level
# ---------------------------------------------------------------------------------------------

class ActorComponent(Object):
    pass


class StaticMeshComponent(ActorComponent):
    _props = {"static_mesh": None}


class PCGComponent(ActorComponent):
    _props = {"is_partitioned": False}

    def __init__(self, outer=None, name=None):
        super().__init__(outer, name)
        self.graph = None
        self.calls = []

    def set_graph(self, graph):
        self.graph = graph

    def generate_local(self, force):
        self.calls.append(("generate_local", force))

    def cleanup_local(self, remove_components, save=False):
        self.calls.append(("cleanup_local", remove_components))


class Actor(Object):
    _props = {"tags": []}
    base_extent = (50.0, 50.0, 50.0)

    def __init__(self, outer=None, name=None, location=(0.0, 0.0, 0.0), extent=None, label=None):
        super().__init__(outer, name)
        self.location = Vector(*location)
        self.scale = Vector(1, 1, 1)
        self.rotation = Rotator()
        self.extent = tuple(extent) if extent is not None else type(self).base_extent
        self.label = label or self._name
        self.components = []

    def get_actor_bounds(self, only_colliding_components, include_from_child_actors=False):
        e, s = self.extent, self.scale
        return (Vector(self.location.x, self.location.y, self.location.z),
                Vector(e[0] * abs(s.x), e[1] * abs(s.y), e[2] * abs(s.z)))

    def set_actor_location(self, new_location, sweep, teleport):
        self.location = new_location

    def set_actor_scale3d(self, new_scale3d):
        self.scale = new_scale3d

    def set_actor_rotation(self, new_rotation, teleport_physics):
        self.rotation = new_rotation
        return True

    def get_actor_label(self):
        return self.label

    def set_actor_label(self, new_actor_label, mark_dirty=True):
        self.label = new_actor_label

    def get_component_by_class(self, component_class):
        return next((c for c in self.components if isinstance(c, component_class)), None)

    def get_components_by_class(self, component_class):
        return [c for c in self.components if isinstance(c, component_class)]


class LandscapeProxy(Actor):
    pass


class Landscape(LandscapeProxy):
    base_extent = (0.0, 0.0, 0.0)  # World Partition: the components live on the streaming proxies


class LandscapeStreamingProxy(LandscapeProxy):
    _props = {"landscape_actor": None}


class StaticMeshActor(Actor):
    def __init__(self, *args, mesh=None, **kwargs):
        super().__init__(*args, **kwargs)
        component = StaticMeshComponent(outer=self)
        component.set_editor_property("static_mesh", mesh)
        self.components.append(component)


class PCGVolume(Actor):
    base_extent = (100.0, 100.0, 100.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.components.append(PCGComponent(outer=self))


class EditorActorSubsystem:
    def __init__(self):
        self.actors = []
        self.selected = []

    def get_all_level_actors(self):
        return list(self.actors)

    def get_selected_level_actors(self):
        return list(self.selected)

    def spawn_actor_from_class(self, actor_class, location, rotation=None, transient=False):
        actor = actor_class(location=(location.x, location.y, location.z))
        self.actors.append(actor)
        return actor


def get_editor_subsystem(cls):
    return _subsystems.setdefault(cls, cls())


# ---------------------------------------------------------------------------------------------
# Assets and editor utilities
# ---------------------------------------------------------------------------------------------

def _asset_key(path):
    path = str(path)
    tail = path.rsplit("/", 1)[-1]
    return path.rsplit(".", 1)[0] if "." in tail else path


def load_asset(path):
    return _assets.get(_asset_key(path))


def register_static_mesh(path):
    mesh = StaticMesh(name=_asset_key(path).rsplit("/", 1)[-1])
    mesh._asset_path = path
    _assets[_asset_key(path)] = mesh
    return mesh


class EditorAssetLibrary:
    saved = []

    @staticmethod
    def does_asset_exist(path):
        return _asset_key(path) in _assets

    @staticmethod
    def load_asset(path):
        return load_asset(path)

    @staticmethod
    def save_loaded_asset(asset, only_if_is_dirty=True):
        EditorAssetLibrary.saved.append(asset)
        return True

    @staticmethod
    def get_metadata_tag(obj, tag):
        return _metadata.get((id(obj), str(tag)), "")

    @staticmethod
    def set_metadata_tag(obj, tag, value):
        _metadata[(id(obj), str(tag))] = str(value)


class AssetTools:
    def create_asset(self, asset_name, package_path, asset_class, factory, calling_context=""):
        asset = asset_class(name=asset_name)
        asset._asset_path = f"{package_path}/{asset_name}.{asset_name}"
        _assets[f"{package_path}/{asset_name}"] = asset
        return asset


class AssetToolsHelpers:
    @staticmethod
    def get_asset_tools():
        return AssetTools()


class EditorUtilityLibrary:
    @staticmethod
    def get_selected_assets():
        return list(_selected_assets)


def select_assets(assets):
    _selected_assets[:] = list(assets)


class ScopedEditorTransaction:
    def __init__(self, description):
        self.description = description

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScopedSlowTask:
    def __init__(self, work, desc="", enabled=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def make_dialog(self, can_cancel=False, allow_in_pie=False):
        pass

    def enter_progress_frame(self, work=1.0, desc=""):
        pass


class SystemLibrary:
    @staticmethod
    def get_engine_version():
        return "5.6.1-fake"


class Paths:
    @staticmethod
    def project_saved_dir():
        return OPTIONS["saved_dir"]

    @staticmethod
    def project_content_dir():
        return OPTIONS["saved_dir"] + "/Content"

    @staticmethod
    def project_intermediate_dir():
        return OPTIONS["saved_dir"] + "/Intermediate"

    @staticmethod
    def convert_relative_path_to_full(path):
        return path


class EditorDialog:
    @staticmethod
    def show_message(title, message, message_type):
        logs.append(("dialog", message))


def register_slate_post_tick_callback(callback):
    tick_callbacks.append(callback)
    return len(tick_callbacks)


def parent_external_window_to_slate(external_window, parent_search_method=None):
    logs.append(("parented", int(external_window)))


def log(message):
    logs.append(("info", message))


def log_warning(message):
    logs.append(("warning", message))


def log_error(message):
    logs.append(("error", message))


# ---------------------------------------------------------------------------------------------
# A small level: a World Partition landscape (2 x 2 km in four proxies) and one kitbash rock
# ---------------------------------------------------------------------------------------------

def make_world(kitbash_tag="PCG_Kitbash"):
    world = get_editor_subsystem(EditorActorSubsystem)
    landscape = Landscape(name="Landscape_0", label="Landscape")
    world.actors.append(landscape)
    for index, (x, y) in enumerate(((-50000, -50000), (50000, -50000), (-50000, 50000), (50000, 50000))):
        proxy = LandscapeStreamingProxy(name=f"LandscapeStreamingProxy_{index}", location=(x, y, 1000),
                                        extent=(50000, 50000, 3000))
        proxy.set_editor_property("landscape_actor", landscape)
        world.actors.append(proxy)
    rock_mesh = register_static_mesh("/Game/Kitbash/SM_Cliff.SM_Cliff")
    rock = StaticMeshActor(name="Cliff_0", location=(1000, 2000, 5000), extent=(800, 800, 2500), mesh=rock_mesh)
    rock.set_editor_property("tags", [Name(kitbash_tag)])
    world.actors.append(rock)
    return world, landscape, rock
