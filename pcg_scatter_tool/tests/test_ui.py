"""UI tests. Run once per binding, e.g. PCG_SCATTER_QT=PySide6 and PCG_SCATTER_QT=PyQt6."""
import os

import pytest

import pcg_scatter_tool as model
from pcg_scatter_tool import BuildReport, LandscapeInfo, OfflineBackend
from pcg_scatter_tool import Bounds, FlatMask, MeshEntry, SurfaceMode

qt_compat = pytest.importorskip("pcg_scatter_tool")
QtWidgets = qt_compat.QtWidgets

import pcg_scatter_tool as ui  # noqa: E402

LANDSCAPE = "/Game/Maps/Island.Island:PersistentLevel.Landscape_0"
PINES = ["/Game/Env/SM_Pine_A.SM_Pine_A", "/Game/Env/SM_Pine_B.SM_Pine_B"]


class StubBackend(OfflineBackend):
    available = True
    name = "stub"

    def __init__(self, folder):
        self.folder = folder
        self.builds = []
        self.tags = []

    def engine_version(self):
        return "5.6.1 (stub)"

    def list_landscapes(self):
        return [LandscapeInfo(LANDSCAPE, "Island")]

    def landscape_bounds(self, landscape_id):
        return Bounds((-204800.0, -204800.0, -5000.0), (204800.0, 204800.0, 30000.0))

    def landscape_layer_names(self, landscape_id):
        return ["Grass", "Rock", "Sand"]

    def selected_landscape_id(self):
        return LANDSCAPE

    def selected_static_mesh_paths(self):
        return list(PINES)

    def tag_selected_actors(self, tag, add=True):
        self.tags.append((tag, add))
        return 2

    def build(self, settings, generate=None):
        self.builds.append((model.settings_to_dict(settings), generate))
        report = BuildReport()
        report.info("stub build")
        report.warn("stub warning")
        return report

    def diagnostics(self):
        return "Unreal Engine 5.6.1 (stub)\nWARNING: example"

    def session_path(self):
        return os.path.join(self.folder, "session.json")


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    win = ui.ScatterToolWindow(StubBackend(str(tmp_path)))
    win.show()
    yield win
    win.close()


def test_opens_with_example_layers(window):
    assert window.layer_list.count() == 3
    assert window.editor.fields["name"].text() == "Trees"
    assert window.settings.landscape == LANDSCAPE
    assert window.landscape_size.text() == "4.10 x 4.10 km"
    assert "About 167,772 candidate points" in window.editor.estimate.text()
    assert window.layer_list.item(0).text().startswith("⚠")  # no meshes yet
    assert window.editor.layer_combo.count() == 3


def test_editing_fields_updates_the_layer(window):
    editor, layer = window.editor, window.settings.layers[0]
    editor.fields["slope_max"].setValue(25.0)
    assert layer.slope_max == 25.0
    assert "0° to 25°" in editor.slope_summary.text()

    flat = editor.fields["flat_mask"]
    flat.setCurrentIndex(flat.findData(FlatMask.NO_FLAT.value))
    editor.fields["flat_threshold"].setValue(10.0)
    assert layer.flat_mask is FlatMask.NO_FLAT
    assert "10° to 25°" in editor.slope_summary.text()

    editor.fields["patch_coverage"].setValue(35.0)
    assert layer.patch_coverage == pytest.approx(0.35)

    assert not editor.paint_group.isEnabled()
    surface = editor.fields["surface"]
    surface.setCurrentIndex(surface.findData(SurfaceMode.LANDSCAPE_ONLY.value))
    assert layer.surface is SurfaceMode.LANDSCAPE_ONLY
    assert editor.paint_group.isEnabled() and editor.avoid_kitbash.isEnabled()

    editor.fields["use_layer_mask"].setChecked(True)
    editor.fields["layer_name"].setCurrentText("Sand")
    assert layer.use_layer_mask and layer.layer_name == "Sand"

    editor.fields["name"].setText("Pines")
    editor.fields["name"].textEdited.emit("Pines")
    assert layer.name == "Pines" and window.layer_list.item(0).text().endswith("Pines")


def test_switching_layers_shows_their_values(window):
    window.layer_list.setCurrentRow(2)
    editor = window.editor
    assert editor.fields["name"].text() == "Grass"
    assert editor.fields["flat_mask"].currentData() == FlatMask.ONLY_FLAT.value
    assert editor.fields["use_patches"].isChecked()
    assert "0° to 20°" in editor.slope_summary.text()
    window.layer_list.setCurrentRow(1)
    assert editor.fields["align_to_surface"].isChecked()
    assert window.settings.layers[2].name == "Grass"  # loading values didn't write back


def test_meshes_from_the_content_browser(window):
    editor, layer = window.editor, window.settings.layers[0]
    editor.meshes._add_from_browser()
    assert [m.path for m in layer.meshes] == PINES
    assert editor.meshes.table.rowCount() == 2
    assert not window.layer_list.item(0).text().startswith("⚠")
    assert editor.issues.isHidden()
    editor.meshes.table.cellWidget(0, 1).setValue(5)
    assert layer.meshes[0].weight == 5
    editor.meshes.table.selectRow(1)
    editor.meshes.remove_selected()
    assert [m.path for m in layer.meshes] == PINES[:1]
    assert editor.meshes.add_paths(PINES) == 1


def test_layer_list_operations(window, monkeypatch):
    window.add_layer()
    assert window.layer_list.count() == 4 and window.current_row() == 3
    window._duplicate_layer()
    assert window.settings.layers[4].name == "Layer 4 copy"
    window._move_layer(-1)
    assert window.settings.layers[3].name == "Layer 4 copy" and window.current_row() == 3
    window.settings.layers[3].meshes = [MeshEntry(PINES[0])]
    monkeypatch.setattr(QtWidgets.QMessageBox, "question", lambda *args, **kwargs: ui.YES)
    window._remove_layer()
    assert [layer.name for layer in window.settings.layers] == ["Trees", "Rocks", "Grass", "Layer 4"]
    window.layer_list.item(0).setCheckState(ui.UNCHECKED)
    assert window.settings.layers[0].enabled is False


def test_preview_and_build(window):
    for index, layer in enumerate(window.settings.layers):
        layer.meshes = [MeshEntry(f"/Game/SM_{index}.SM_{index}", 1)]
    text = window.preview()
    assert "Surface Sampler" in text and "Layer 3: Grass" in text
    assert window.build(True) is not None
    data, generate = window.backend.builds[-1]
    assert generate is True and data["landscape"] == LANDSCAPE
    log = window.log_view.toPlainText()
    assert "stub build" in log and "stub warning" in log
    assert os.path.isfile(window.backend.session_path())


def test_build_is_blocked_by_invalid_settings(window, monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *args, **kwargs: shown.append(args))
    window.graph_edit.setText("not a path")
    window.graph_edit.editingFinished.emit()
    assert window.build(False) is None
    assert shown and window.backend.builds == []


def test_target_fields_and_tagging(window):
    window.kitbash_edit.setText("Kitbash")
    window.kitbash_edit.editingFinished.emit()
    window.partition_check.setChecked(True)
    window.margin_spin.setValue(1234.0)
    assert window.settings.kitbash_tag == "Kitbash" and window.settings.partitioned
    assert window.settings.volume_margin == 1234.0
    window._tag(window.kitbash_edit, True)
    assert window.backend.tags == [("Kitbash", True)]
    assert "Tagged 2 selected actor(s) with 'Kitbash'" in window.log_view.toPlainText()


def test_presets_and_session(app, tmp_path):
    backend = StubBackend(str(tmp_path))
    first = ui.ScatterToolWindow(backend)
    first.settings.layers[1].name = "Boulders"
    preset = tmp_path / "preset.json"
    model.save_settings(first.settings, str(preset))
    first.save_session()
    first.close()

    second = ui.ScatterToolWindow(backend)
    assert second.settings.layers[1].name == "Boulders"
    loaded = model.load_settings(str(preset))
    loaded.layers = loaded.layers[:1]
    second.set_settings(loaded)
    assert second.layer_list.count() == 1 and second.settings.landscape == LANDSCAPE
    second.close()


def test_offline_window_disables_editor_actions(app, tmp_path, monkeypatch):
    monkeypatch.setattr(OfflineBackend, "session_path", lambda self: str(tmp_path / "offline.json"))
    win = ui.ScatterToolWindow(OfflineBackend())
    assert not win.generate_button.isEnabled() and not win.build_button.isEnabled()
    assert "Offline" in win.statusBar().currentMessage()
    assert win.preview().startswith("Nothing to build")
    win.close()


def test_diagnostics_and_painting(window):
    window._diagnostics()
    assert "WARNING: example" in window.log_view.toPlainText()
    for row in range(3):
        window.layer_list.setCurrentRow(row)
        assert not window.editor.slope_bar.grab().isNull()
    assert not window.grab().isNull()


def test_running_the_file_in_unreal_opens_the_window(app, tmp_path):
    """What `py "pcg_scatter_tool.py"` does in the editor: run the file as __main__ with `unreal` present."""
    import runpy
    import sys

    import fake_unreal

    fake_unreal.reset(saved_dir=str(tmp_path))
    fake_unreal.make_world()
    sys.modules.pop("_pcg_scatter_tool_state", None)
    try:
        runpy.run_path(ui.__file__, run_name="__main__")
        state = sys.modules["_pcg_scatter_tool_state"]
        first = state.window
        assert first.isVisible() and first.backend.available
        assert first.landscape_combo.currentText() == "Landscape"
        assert any(level == "parented" for level, _ in fake_unreal.logs)
        assert len(fake_unreal.tick_callbacks) == 1
        fake_unreal.tick_callbacks[0](0.016)  # lets Qt process its events

        runpy.run_path(ui.__file__, run_name="__main__")  # running it again replaces the window
        assert state.window is not first and not first.isVisible()
        assert len(fake_unreal.tick_callbacks) == 1
        state.window.close()
    finally:
        sys.modules.pop("_pcg_scatter_tool_state", None)
