import math

import pytest

import fake_unreal as ue
import pcg_scatter_tool as model
import pcg_scatter_tool as unreal_backend
from pcg_scatter_tool import MeshEntry

GRAPH = "/Game/PCG/PCG_Scatter"
SOURCES = (ue.PCGGetLandscapeSettings, ue.PCGWorldRayHitSettings, ue.PCGCreatePointsSettings, ue.PCGDataFromActorSettings)


@pytest.fixture
def world(tmp_path):
    ue.reset(saved_dir=str(tmp_path))
    return ue.make_world()


def ready_settings(landscape):
    settings = model.default_settings()
    for index, layer in enumerate(settings.layers):
        paths = [f"/Game/Env/SM_{index}_{n}.SM_{index}_{n}" for n in range(2)]
        for path in paths:
            ue.register_static_mesh(path)
        layer.meshes = [MeshEntry(paths[0], 3), MeshEntry(paths[1], 1)]
    settings.landscape = landscape.get_path_name()
    return settings


def build(settings, **kwargs):
    return unreal_backend.UnrealBackend().build(settings, **kwargs)


def graph():
    return ue.load_asset(GRAPH)


def assert_fully_connected(g):
    for node in g.nodes():
        settings = node.get_settings()
        if not isinstance(settings, SOURCES):
            assert g.incoming(node), f"{type(settings).__name__} has no input"
        if not isinstance(settings, ue.PCGStaticMeshSpawnerSettings):
            assert g.outgoing(node), f"{type(settings).__name__} has no output"


def density_windows(g):
    return sorted((round(n.get_settings().get_editor_property("lower_bound"), 4),
                   round(n.get_settings().get_editor_property("upper_bound"), 4))
                  for n in g.nodes_of(ue.PCGDensityFilterSettings))


def test_build_creates_a_connected_graph(world):
    _, landscape, _ = world
    report = build(ready_settings(landscape), generate=True)
    assert report.errors == [], report.text()
    g = graph()
    assert ue.EditorAssetLibrary.get_metadata_tag(g, unreal_backend.TOOL_METADATA_KEY) == "1"
    assert_fully_connected(g)

    spawners = g.nodes_of(ue.PCGStaticMeshSpawnerSettings)
    assert len(spawners) == 3
    for spawner in spawners:
        entries = spawner.get_settings().get_editor_property("mesh_selector_parameters").get_editor_property("mesh_entries")
        assert [e.get_editor_property("weight") for e in entries] == [3, 1]
        assert entries[0].get_editor_property("descriptor").get_editor_property("static_mesh") is not None

    # Samplers take the surface and the graph input (the fake's Input node only has an "In" pin).
    for sampler in g.nodes_of(ue.PCGSurfaceSamplerSettings):
        assert {edge[3] for edge in g.incoming(sampler)} == {"Surface", "Bounding Shape"}
    assert any(edge[0] is g.get_input_node() and edge[1] == "In" for edge in g.edges)

    windows = density_windows(g)
    cos = lambda degrees: round(math.cos(math.radians(degrees)), 4)  # noqa: E731
    assert (cos(30), 1.0) in windows  # trees 0-30
    assert (cos(70), cos(15)) in windows  # rocks 15-70
    assert (cos(20), 1.0) in windows  # grass: only flat, below 20
    assert (0.4, 1.0) in windows  # grass patches at 60% coverage


def test_graph_settings_follow_the_layers(world):
    _, landscape, _ = world
    settings = ready_settings(landscape)
    settings.layers[1].surface = model.SurfaceMode.KITBASH_ONLY
    build(settings)
    g = graph()
    queries = [n.get_settings().get_editor_property("query_params") for n in g.nodes_of(ue.PCGWorldRayHitSettings)]
    filters = sorted(q.get_editor_property("actor_tag_filter") for q in queries)
    assert filters == ["PCGWorldQueryFilterByTag.EXCLUDE_TAGGED", "PCGWorldQueryFilterByTag.INCLUDE_TAGGED"]
    kitbash = next(q for q in queries if q.get_editor_property("actor_tag_filter").endswith("INCLUDE_TAGGED"))
    assert set(kitbash.get_editor_property("actor_tags_list")) == {"PCG_Kitbash"}
    assert kitbash.get_editor_property("select_landscape_hits") == "PCGWorldQuerySelectLandscapeHits.EXCLUDE"

    transforms = [n.get_settings() for n in g.nodes_of(ue.PCGTransformPointsSettings)]
    assert sorted(t.get_editor_property("absolute_rotation") for t in transforms) == [False, False, True]
    grass_spawner = g.nodes_of(ue.PCGStaticMeshSpawnerSettings)[2].get_settings()
    descriptor = grass_spawner.get_editor_property("mesh_selector_parameters").get_editor_property("mesh_entries")[0] \
        .get_editor_property("descriptor")
    assert descriptor.get_editor_property("instance_end_cull_distance") == 6000
    assert descriptor.get_editor_property("cast_shadow") is False
    assert descriptor.get_editor_property("body_instance").get_editor_property("collision_enabled") == "CollisionEnabled.NO_COLLISION"

    positions = {n.position for n in g.nodes()}
    assert None not in positions and len(positions) == len(g.nodes())


def test_volume_is_fitted_and_generated(world):
    actors, landscape, _ = world
    report = build(ready_settings(landscape), generate=True)
    assert report.errors == []
    volumes = [a for a in actors.actors if isinstance(a, ue.PCGVolume)]
    assert len(volumes) == 1
    volume = volumes[0]
    # Landscape: 2 x 2 km, z -2000..4000; the tagged cliff reaches z 7500; margin 5000 cm.
    assert (volume.scale.x, volume.scale.y) == (pytest.approx(1000.0), pytest.approx(1000.0))
    assert volume.scale.z == pytest.approx((4750 + 5000) / 100)
    assert volume.location.z == pytest.approx(2750)
    component = volume.get_component_by_class(ue.PCGComponent)
    assert component.graph is graph()
    assert component.calls == [("cleanup_local", True), ("generate_local", True)]


def test_rebuild_replaces_nodes_and_reuses_the_volume(world):
    actors, landscape, _ = world
    settings = ready_settings(landscape)
    build(settings)
    first = len(graph().nodes())
    settings.layers[0].use_height = True
    build(settings)
    assert len(graph().nodes()) == first + 2
    build(settings, generate=False)
    assert len(graph().nodes()) == first + 2
    assert len([a for a in actors.actors if isinstance(a, ue.PCGVolume)]) == 1


def test_refuses_to_overwrite_a_graph_it_did_not_create(world):
    _, landscape, _ = world
    ue.AssetToolsHelpers.get_asset_tools().create_asset("PCG_Scatter", "/Game/PCG", ue.PCGGraph, None)
    report = build(ready_settings(landscape))
    assert any("did not create" in error for error in report.errors)
    assert graph().nodes() == []


def test_missing_optional_node_is_bridged(world, monkeypatch):
    _, landscape, _ = world
    monkeypatch.delattr(ue, "PCGSpatialNoiseSettings")
    report = build(ready_settings(landscape))
    assert report.errors == []
    assert any("Spatial Noise" in warning and "patches" in warning for warning in report.warnings)
    assert any("Patch filter" in warning for warning in report.warnings)  # depends on the noise node
    assert (0.4, 1.0) not in density_windows(graph())
    assert_fully_connected(graph())


def test_density_filter_falls_back_to_attribute_range(world, monkeypatch):
    _, landscape, _ = world
    monkeypatch.delattr(ue, "PCGDensityFilterSettings")
    report = build(ready_settings(landscape))
    assert report.errors == [] and report.warnings == []
    g = graph()
    ranges = g.nodes_of(ue.PCGAttributeFilteringRangeSettings)
    assert len(ranges) == 4
    for node in ranges:
        selector = node.get_settings().get_editor_property("target_attribute")
        assert "Selection=PointProperty" in selector.export_text()
        assert {edge[1] for edge in g.outgoing(node)} == {"InsideFilter"}
    assert_fully_connected(g)


@pytest.mark.parametrize("options", [{}, {"selector_import_fails": True, "selector_helpers": True}])
def test_paint_layer_mask(tmp_path, options):
    ue.reset(saved_dir=str(tmp_path), **options)
    _, landscape, _ = ue.make_world()
    settings = ready_settings(landscape)
    settings.layers[2].use_layer_mask, settings.layers[2].layer_name = True, "Grass"
    report = build(settings)
    assert report.errors == [] and report.warnings == []
    mask = graph().nodes_of(ue.PCGAttributeFilteringRangeSettings)
    assert len(mask) == 1
    assert 'AttributeName="Grass"' in mask[0].get_settings().get_editor_property("target_attribute").export_text()
    minimum = mask[0].get_settings().get_editor_property("min_threshold")
    assert minimum.get_editor_property("attribute_types").get_editor_property("double_value") == 0.5


def test_paint_layer_is_skipped_when_selectors_cannot_be_set(tmp_path):
    ue.reset(saved_dir=str(tmp_path), selector_import_fails=True)
    _, landscape, _ = ue.make_world()
    settings = ready_settings(landscape)
    settings.layers[2].use_layer_mask, settings.layers[2].layer_name = True, "Grass"
    report = build(settings)
    assert report.errors == []
    assert any("paint-layer mask is not applied" in warning for warning in report.warnings)
    assert graph().nodes_of(ue.PCGAttributeFilteringRangeSettings) == []
    assert_fully_connected(graph())


def test_input_pin_named_input(tmp_path):
    ue.reset(saved_dir=str(tmp_path), input_node_pins=("Input",))
    _, landscape, _ = ue.make_world()
    build(ready_settings(landscape))
    assert any(edge[0] is graph().get_input_node() and edge[1] == "Input" for edge in graph().edges)


def test_missing_meshes_are_reported(world):
    _, landscape, _ = world
    settings = ready_settings(landscape)
    settings.layers[0].meshes.append(MeshEntry("/Game/Nope/SM_Missing.SM_Missing", 1))
    report = build(settings)
    assert any("SM_Missing" in warning for warning in report.warnings)
    assert report.errors == []


def test_volume_without_brush_is_an_error(world, monkeypatch):
    _, landscape, _ = world
    monkeypatch.setattr(ue.PCGVolume, "base_extent", (0.0, 0.0, 0.0))
    report = build(ready_settings(landscape))
    assert any("no brush" in error for error in report.errors)


def test_landscape_queries(world):
    actors, landscape, cliff = world
    backend = unreal_backend.UnrealBackend()
    assert [info.id for info in backend.list_landscapes()] == [landscape.get_path_name()]
    bounds = backend.landscape_bounds(landscape.get_path_name())
    assert bounds.min == (-100000.0, -100000.0, -2000.0) and bounds.max == (100000.0, 100000.0, 4000.0)
    actors.selected = [actors.actors[2]]  # a streaming proxy
    assert backend.selected_landscape_id() == landscape.get_path_name()
    actors.selected = [cliff]
    assert backend.selected_actor_mesh_paths() == ["/Game/Kitbash/SM_Cliff.SM_Cliff"]
    ue.select_assets([ue.load_asset("/Game/Kitbash/SM_Cliff"), landscape])
    assert backend.selected_static_mesh_paths() == ["/Game/Kitbash/SM_Cliff.SM_Cliff"]


def test_tagging_selected_actors(world):
    actors, landscape, cliff = world
    backend = unreal_backend.UnrealBackend()
    actors.selected = [cliff, landscape]
    assert backend.tag_selected_actors("PCG_NoScatter") == 2
    assert backend.tag_selected_actors("PCG_NoScatter") == 0
    assert backend.tag_selected_actors("PCG_Kitbash", add=False) == 1
    assert [str(t) for t in cliff.get_editor_property("tags")] == ["PCG_NoScatter"]


def test_cleanup(world):
    actors, landscape, _ = world
    backend = unreal_backend.UnrealBackend()
    assert backend.cleanup(ready_settings(landscape)).warnings
    backend.build(ready_settings(landscape), generate=False)
    report = backend.cleanup(ready_settings(landscape))
    assert report.errors == []
    volume = next(a for a in actors.actors if isinstance(a, ue.PCGVolume))
    assert volume.get_component_by_class(ue.PCGComponent).calls == [("cleanup_local", True)]


def test_diagnostics(world):
    text = unreal_backend.UnrealBackend().diagnostics()
    assert "Unreal Engine 5.6.1-fake" in text
    assert "Selector $Density: ok" in text and "Selector Grass: ok" in text
    assert "Node types with no class in this engine: none" in text
    assert "ERROR" not in text
    assert ue.load_asset("/Game/PCGScatterCheck/PCG_Check") is None  # nothing saved


def test_output_log_gets_the_report(world):
    _, landscape, _ = world
    build(ready_settings(landscape))
    assert any(text.startswith("[PCG Scatter] Built 3 layer(s)") for level, text in ue.logs)
