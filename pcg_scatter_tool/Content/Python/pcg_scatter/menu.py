"""Adds Tools > PCG Scatter Tool to the Level Editor menu. Called from init_unreal.py."""
from __future__ import annotations

MENU_NAME = "LevelEditor.MainMenu.Tools"
SECTION_NAME = "PCGScatterTool"


def register_menu() -> bool:
    import unreal

    menus = unreal.ToolMenus.get()
    menu = menus.find_menu(MENU_NAME)
    if menu is None:
        unreal.log_warning("[PCG Scatter] Tools menu not found. Open the tool from the Output Log (Python) "
                           "with: import pcg_scatter; pcg_scatter.launch()")
        return False
    entry = unreal.ToolMenuEntry(name="PCGScatterToolOpen", type=unreal.MultiBlockType.MENU_ENTRY)
    entry.set_label("PCG Scatter Tool")
    entry.set_tool_tip("Build a PCG scatter graph for a landscape and the kitbash meshes on it.")
    entry.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "",
                             "import pcg_scatter; pcg_scatter.launch()")
    menu.add_menu_entry(SECTION_NAME, entry)
    menus.refresh_all_widgets()
    return True
