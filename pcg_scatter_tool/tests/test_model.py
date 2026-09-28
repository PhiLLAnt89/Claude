import math

import pytest

import pcg_scatter_tool as model
from pcg_scatter_tool import FlatMask, MeshEntry, ScatterLayer, SurfaceMode


def layer(**kwargs):
    base = dict(meshes=[MeshEntry("/Game/SM_A.SM_A", 1)])
    base.update(kwargs)
    return ScatterLayer(**base)


@pytest.mark.parametrize("flat, threshold, expected", [
    (FlatMask.ANY, 10.0, (5.0, 40.0)),
    (FlatMask.ONLY_FLAT, 10.0, (5.0, 10.0)),
    (FlatMask.NO_FLAT, 10.0, (10.0, 40.0)),
    (FlatMask.ONLY_FLAT, 60.0, (5.0, 40.0)),
])
def test_effective_slope_range_combines_flat_rule(flat, threshold, expected):
    assert model.effective_slope_range(layer(slope_min=5, slope_max=40, flat_mask=flat, flat_threshold=threshold)) == expected


def test_contradicting_rules_leave_no_slope_and_are_an_error():
    broken = layer(slope_min=30, slope_max=60, flat_mask=FlatMask.ONLY_FLAT, flat_threshold=10)
    low, high = model.effective_slope_range(broken)
    assert low > high
    assert any("no valid slope" in issue.message for issue in model.validate_layer(broken))


def test_slope_density_bounds_are_cosines():
    lower, upper = model.slope_density_bounds(15.0, 60.0)
    assert lower == pytest.approx(math.cos(math.radians(60)), abs=1e-5)
    assert upper == pytest.approx(math.cos(math.radians(15)), abs=1e-5)
    assert model.slope_density_bounds(0.0, 90.0) == (0.0, 1.0)


def test_swapped_slope_limits_are_sorted():
    assert model.effective_slope_range(layer(slope_min=50, slope_max=20)) == (20.0, 50.0)


def test_validation_messages():
    assert any(i.level == "error" and "static mesh" in i.message for i in model.validate_layer(ScatterLayer()))
    zero = layer(meshes=[MeshEntry("/Game/SM_A.SM_A", 0)])
    assert any("weights are 0" in i.message for i in model.validate_layer(zero))
    heights = layer(use_height=True, height_min=100, height_max=100)
    assert any("Height" in i.message for i in model.validate_layer(heights))
    paint = layer(use_layer_mask=True, layer_name="Grass", surface=SurfaceMode.LANDSCAPE_AND_KITBASH)
    assert any(i.level == "warning" and "Landscape only" in i.message for i in model.validate_layer(paint))
    no_tag = model.ToolSettings(kitbash_tag="")
    assert any(i.level == "error" for i in model.validate_layer(layer(surface=SurfaceMode.KITBASH_ONLY), no_tag))
    assert model.validate_layer(layer()) == []


def test_settings_validation():
    settings = model.default_settings()
    settings.landscape = "/Game/Map.Map:PersistentLevel.Landscape_0"
    assert model.validate_settings(settings) == []
    settings.graph_path = "Game/no leading slash"
    assert any("asset path" in i.message for i in model.validate_settings(settings))
    settings.graph_path = "/Game/PCG/Graph"
    settings.landscape = ""
    assert any("landscape" in i.message.lower() for i in model.validate_settings(settings))
    assert model.validate_settings(settings, require_landscape=False) == []
    settings.no_scatter_tag = settings.kitbash_tag
    assert any("different" in i.message for i in model.validate_settings(settings, require_landscape=False))


@pytest.mark.parametrize("path, expected", [
    ("/Game/PCG/PCG_Scatter", ("/Game/PCG", "PCG_Scatter")),
    ("/Game/PCG/PCG_Scatter.PCG_Scatter", ("/Game/PCG", "PCG_Scatter")),
    ("/MyPlugin/Graphs/G1", ("/MyPlugin/Graphs", "G1")),
])
def test_split_asset_path(path, expected):
    assert model.split_asset_path(path) == expected


@pytest.mark.parametrize("path", ["", "/Game", "Game/PCG/X", "/Game/PCG/Bad Name", "/Game/PCG/"])
def test_split_asset_path_rejects(path):
    with pytest.raises(ValueError):
        model.split_asset_path(path)


def test_preset_round_trip(tmp_path):
    settings = model.default_settings()
    settings.layers[0].meshes = [MeshEntry("/Game/Trees/SM_Pine.SM_Pine", 3)]
    settings.layers[2].flat_mask = FlatMask.NO_FLAT
    settings.partitioned = True
    path = tmp_path / "preset.json"
    model.save_settings(settings, str(path))
    loaded = model.load_settings(str(path))
    assert model.settings_to_dict(loaded) == model.settings_to_dict(settings)
    assert loaded.layers[2].flat_mask is FlatMask.NO_FLAT
    assert loaded.layers[0].meshes[0].weight == 3


def test_loading_tolerates_unknown_and_bad_values():
    data = model.settings_to_dict(model.default_settings())
    data["something_new"] = 1
    data["layers"][0]["density"] = "not a number"
    data["layers"][0]["surface"] = "no_such_mode"
    data["layers"][0]["future_field"] = [1, 2]
    loaded = model.settings_from_dict(data)
    assert loaded.layers[0].density == ScatterLayer().density
    assert loaded.layers[0].surface is SurfaceMode.LANDSCAPE_AND_KITBASH


def test_bounds_helpers():
    a = model.Bounds.from_origin_extent((0, 0, 0), (100, 200, 50))
    b = model.Bounds((50, 50, -500), (400, 400, 10))
    assert a.size == (200, 400, 100)
    assert a.area_m2 == pytest.approx(8.0)
    assert a.union(b).min == (-100, -200, -500)
    assert a.overlaps_xy(b)
    assert not a.overlaps_xy(model.Bounds((1000, 1000, 0), (2000, 2000, 1)))


def test_estimate_samples():
    assert model.estimate_samples(layer(density=0.5), 1_000_000.0) == 500_000
    assert model.estimate_samples(layer(), None) is None
