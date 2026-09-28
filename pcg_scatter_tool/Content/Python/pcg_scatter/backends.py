"""Types shared by the UI and the backends, plus the offline backend used outside Unreal.

A backend is whatever the window talks to for editor work. UnrealBackend (unreal_backend.py)
does the real thing; OfflineBackend lets the window open outside the editor to edit presets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import Bounds, ToolSettings

INFO, WARNING, ERROR, DETAIL = "info", "warning", "error", "detail"


@dataclass(frozen=True)
class LandscapeInfo:
    id: str  # actor path name
    label: str


class BuildReport:
    """Collects what happened during a build instead of raising, so partial results are visible."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.lines.append((INFO, message))

    def warn(self, message: str) -> None:
        self.lines.append((WARNING, message))

    def error(self, message: str) -> None:
        self.lines.append((ERROR, message))

    def detail(self, message: str) -> None:
        """Only shown in the diagnostics output."""
        self.lines.append((DETAIL, message))

    @property
    def errors(self) -> List[str]:
        return [text for level, text in self.lines if level == ERROR]

    @property
    def warnings(self) -> List[str]:
        return [text for level, text in self.lines if level == WARNING]

    def visible_lines(self, verbose: bool = False) -> List[Tuple[str, str]]:
        return [(level, text) for level, text in self.lines if verbose or level != DETAIL]

    def text(self, verbose: bool = False) -> str:
        prefix = {INFO: "", WARNING: "WARNING: ", ERROR: "ERROR: ", DETAIL: "    "}
        return "\n".join(prefix[level] + text for level, text in self.visible_lines(verbose))

    def summary(self) -> str:
        if self.errors:
            return f"Finished with {len(self.errors)} error(s) and {len(self.warnings)} warning(s)."
        if self.warnings:
            return f"Done with {len(self.warnings)} warning(s)."
        return "Done."


class OfflineBackend:
    """Stand-in used when the window runs outside Unreal (preset editing only)."""

    available = False
    name = "Offline (outside Unreal)"

    def engine_version(self) -> str:
        return ""

    def list_landscapes(self) -> List[LandscapeInfo]:
        return []

    def landscape_bounds(self, landscape_id: str) -> Optional[Bounds]:
        return None

    def landscape_layer_names(self, landscape_id: str) -> List[str]:
        return []

    def selected_landscape_id(self) -> Optional[str]:
        return None

    def selected_static_mesh_paths(self) -> List[str]:
        return []

    def selected_actor_mesh_paths(self) -> List[str]:
        return []

    def tag_selected_actors(self, tag: str, add: bool = True) -> int:
        raise RuntimeError("Tagging actors only works inside the Unreal Editor.")

    def build(self, settings: ToolSettings, generate: Optional[bool] = None) -> BuildReport:
        raise RuntimeError("Building only works inside the Unreal Editor.")

    def cleanup(self, settings: ToolSettings) -> BuildReport:
        raise RuntimeError("Clean up only works inside the Unreal Editor.")

    def diagnostics(self) -> str:
        return "The PCG API check only runs inside the Unreal Editor."

    def session_path(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".pcg_scatter_tool", "last_session.json")
