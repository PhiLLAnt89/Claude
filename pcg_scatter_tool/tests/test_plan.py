import pcg_scatter_tool as model
import pcg_scatter_tool as planlib
from pcg_scatter_tool import FlatMask, MeshEntry, SurfaceMode


def ready_settings():
    settings = model.default_settings()
    for index, layer in enumerate(settings.layers):
        layer.meshes = [MeshEntry(f"/Game/Env/SM_{index}.SM_{index}", 1)]
    return settings


def classes(plan, layer_name):
    return [spec.variants[0].classes[0] for spec in plan.nodes if spec.layer == layer_name]


def test_default_plan_builds_every_layer():
    plan = planlib.build_plan(ready_settings())
    assert plan.layers == ["Trees", "Rocks", "Grass"]
    assert plan.notes == []
    for name in plan.layers:
        chain = classes(plan, name)
        assert chain[1] == "PCGSurfaceSamplerSettings"
        assert chain[-2:] == ["PCGTransformPointsSettings", "PCGStaticMeshSpawnerSettings"]
    keys = {spec.key for spec in plan.nodes}
    for edge in plan.edges:
        assert edge.src in keys or edge.src == planlib.INPUT_NODE
        assert edge.dst in keys


def test_layers_without_meshes_are_skipped_with_a_note():
    plan = planlib.build_plan(model.default_settings())
    assert plan.nodes == []
    assert len(plan.notes) == 3 and all("static mesh" in note for note in plan.notes)


def test_surface_modes():
    settings = ready_settings()
    settings.layers[0].surface = SurfaceMode.KITBASH_ONLY
    plan = planlib.build_plan(settings)
    ray = plan.node("L1.surface")
    props = {p.path: p.value for p in ray.variants[0].props}
    assert props["query_params.actor_tag_filter"].members == ("INCLUDE_TAGGED",)
    assert props["query_params.actor_tags_list"].names == ("PCG_Kitbash",)
    rocks = {p.path: p.value for p in plan.node("L2.surface").variants[0].props}
    assert rocks["query_params.actor_tag_filter"].members == ("EXCLUDE_TAGGED",)
    assert rocks["query_params.actor_tags_list"].names == ("PCG_NoScatter",)
    assert plan.node("L3.surface").variants[0].classes == ("PCGGetLandscapeSettings",)


def test_full_slope_range_adds_no_slope_nodes():
    settings = ready_settings()
    trees = settings.layers[0]
    trees.slope_min, trees.slope_max, trees.flat_mask = 0.0, 90.0, FlatMask.ANY
    assert "PCGNormalToDensitySettings" not in classes(planlib.build_plan(settings), "Trees")


def test_slope_filter_uses_cosine_bounds_and_has_a_fallback():
    settings = ready_settings()
    plan = planlib.build_plan(settings)
    slope = plan.node("L2.slope_filter")  # rocks: 15-70 degrees
    first = {p.path: p.value for p in slope.variants[0].props}
    lower, upper = model.slope_density_bounds(15.0, 70.0)
    assert (first["lower_bound"], first["upper_bound"]) == (lower, upper)
    assert slope.requires == ("L2.slope_density",)
    fallback = slope.variants[1]
    assert fallback.classes == planlib.ATTRIBUTE_RANGE_CLASSES
    assert fallback.main_out == planlib.FILTER_INSIDE_PIN


def test_later_layers_keep_clear_of_earlier_ones():
    settings = ready_settings()
    assert "L3.avoid_layers" not in {spec.key for spec in planlib.build_plan(settings).nodes}  # off for grass
    settings.layers[2].avoid_previous_layers = True
    plan = planlib.build_plan(settings)
    grass_avoid = plan.node("L3.avoid_layers")
    sources = [e.src for e in plan.edges if e.dst == grass_avoid.key and e.dst_pin == planlib.DIFFERENCES_PIN]
    # Each layer's last point step before Transform Points: trees end at spacing, rocks at their
    # own avoid step (they keep clear of the trees).
    assert sources == ["L1.spacing", "L2.avoid_layers"]
    assert not any(spec.key == "L1.avoid_layers" for spec in plan.nodes)


def test_optional_features_add_helper_nodes():
    settings = ready_settings()
    grass = settings.layers[2]
    grass.use_height, grass.height_min, grass.height_max = True, -1000.0, 25000.0
    grass.use_layer_mask, grass.layer_name = True, "Grass"
    plan = planlib.build_plan(settings)
    chain = classes(plan, "Grass")
    for expected in ("PCGCreatePointsSettings", "PCGDifferenceSettings", "PCGSpatialNoiseSettings",
                     "PCGDataFromActorSettings", "PCGAttributeFilteringRangeSettings"):
        assert expected in chain
    boxes = {p.path: p.value for p in plan.node("L3.height_limits").variants[0].props}["points_to_create"]
    assert boxes.boxes[0].max[2] == -1000.0 and boxes.boxes[1].min[2] == 25000.0


def test_paint_layer_needs_landscape_surface():
    settings = ready_settings()
    settings.layers[0].use_layer_mask, settings.layers[0].layer_name = True, "Grass"
    assert "L1.paint_layer" not in {spec.key for spec in planlib.build_plan(settings).nodes}


def test_transform_orientation():
    plan = planlib.build_plan(ready_settings())
    trees = {p.path: p.value for p in plan.node("L1.transform").variants[0].props}
    rocks = {p.path: p.value for p in plan.node("L2.transform").variants[0].props}
    assert trees["absolute_rotation"] is True and rocks["absolute_rotation"] is False
    assert trees["rotation_max"].yaw == 360.0 and trees["rotation_max"].pitch == 3.0
    assert trees["offset_min"].z == -20.0


def test_describe_lists_nodes():
    text = planlib.build_plan(ready_settings()).describe()
    assert "Layer 1: Trees" in text and "Surface Sampler" in text and "Static Mesh Spawner" in text


def test_diagnostic_settings_cover_every_node_type():
    plan = planlib.build_plan(model.diagnostic_settings())
    assert plan.notes == []
    names = set(plan.class_names())
    for expected in ("PCGGetLandscapeSettings", "PCGWorldRayHitSettings", "PCGSpatialNoiseSettings",
                     "PCGCreatePointsSettings", "PCGDataFromActorSettings", "PCGSelfPruningSettings"):
        assert expected in names
