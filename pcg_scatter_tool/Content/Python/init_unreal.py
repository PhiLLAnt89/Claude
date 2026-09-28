"""Unreal runs this file when the editor starts; it adds Tools > PCG Scatter Tool.

If your project already has Content/Python/init_unreal.py, paste the lines below into it instead
of replacing it.
"""
import unreal

try:
    import pcg_scatter.menu

    pcg_scatter.menu.register_menu()
except Exception as exc:  # never break editor start-up
    unreal.log_warning(f"[PCG Scatter] Tools menu entry not added: {exc}")
