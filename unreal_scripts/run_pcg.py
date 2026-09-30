"""run_pcg.py - generate, clean up or regenerate the PCG components in the open Unreal level.

Run it in the Unreal Editor's Output Log with the input box set to "Cmd" (UE 5.x, PCG plugin enabled):

    py "D:/Tools/run_pcg.py"                      generate every PCG component in the level
    py "D:/Tools/run_pcg.py" --selected           only the actors selected in the level
    py "D:/Tools/run_pcg.py" --label Forest       only actors whose label contains "Forest"
    py "D:/Tools/run_pcg.py" --tag PCG_Kitbash    only actors carrying that tag
    py "D:/Tools/run_pcg.py" --force              clean up first, then generate again
    py "D:/Tools/run_pcg.py" --cleanup            remove everything the components generated
    py "D:/Tools/run_pcg.py" --save               save the level when generation has finished
    py "D:/Tools/run_pcg.py" --list               just list the PCG components and their state

Generation runs in the background over the next editor frames; the script watches it and prints a
line in the Output Log when everything is finished (search the log for "[run_pcg]").

You can also call it from Python:  import run_pcg; run_pcg.run(force=True, save=True)
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable, List, Optional

import unreal

PREFIX = "[run_pcg] "
_watchers: List[Any] = []  # keeps tick callbacks alive until they unregister themselves


def log(text: str, level: str = "info") -> None:
    {"info": unreal.log, "warning": unreal.log_warning, "error": unreal.log_error}[level](PREFIX + text)


# ---------------------------------------------------------------------------------------------
# Finding components
# ---------------------------------------------------------------------------------------------

def _label(actor: Any) -> str:
    try:
        return str(actor.get_actor_label())
    except Exception:
        return str(actor.get_name())


def _tags(actor: Any) -> List[str]:
    try:
        return [str(tag) for tag in actor.get_editor_property("tags")]
    except Exception:
        return []


def find_components(selected: bool = False, label: str = "", tag: str = "") -> List[Any]:
    """PCG components on the level's actors, filtered by selection, label substring or actor tag."""
    if not hasattr(unreal, "PCGComponent"):
        log("The PCG plugin is not loaded. Enable 'Procedural Content Generation Framework' and restart.", "error")
        return []
    actors_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actors = actors_subsystem.get_selected_level_actors() if selected else actors_subsystem.get_all_level_actors()
    components = []
    for actor in actors or []:
        if label and label.lower() not in _label(actor).lower():
            continue
        if tag and tag not in _tags(actor):
            continue
        try:
            found = actor.get_components_by_class(unreal.PCGComponent) or []
        except Exception:
            found = []
        components.extend(found)
    return components


def component_name(component: Any) -> str:
    try:
        owner = component.get_owner()
        return f"{_label(owner)}/{component.get_name()}"
    except Exception:
        return str(component.get_name())


def component_graph(component: Any) -> Any:
    for getter in ("get_graph", "get_graph_instance"):
        method = getattr(component, getter, None)
        if method is not None:
            try:
                return method()
            except Exception:
                continue
    try:
        return component.get_editor_property("graph_instance")
    except Exception:
        return None


def is_generating(component: Any) -> bool:
    method = getattr(component, "is_generating", None)
    if method is None:
        return False
    try:
        return bool(method())
    except Exception:
        return False


def _call_first(component: Any, attempts: List[tuple]) -> bool:
    for name, args in attempts:
        method = getattr(component, name, None)
        if method is None:
            continue
        try:
            method(*args)
            return True
        except Exception as exc:  # try the next spelling of the call
            log(f"{name}{args} failed on {component_name(component)}: {exc}", "warning")
    return False


def generate(component: Any, force: bool) -> bool:
    return _call_first(component, [("generate_local", (force,)), ("generate", (force,))])


def cleanup(component: Any) -> bool:
    return _call_first(component, [("cleanup_local", (True,)), ("cleanup", (True,))])


# ---------------------------------------------------------------------------------------------
# Waiting for generation without blocking the editor
# ---------------------------------------------------------------------------------------------

def watch(components: List[Any], on_done: Optional[Callable[[], None]] = None, timeout: float = 600.0) -> None:
    """Polls the components every editor frame and logs when generation has finished.

    Blocking with time.sleep would stall the very frames PCG needs, so this uses a Slate tick.
    """
    started = time.time()
    state = {"handle": None, "last_report": started, "done": False}

    def tick(_delta: float) -> None:
        if state["done"]:
            return
        busy = [c for c in components if is_generating(c)]
        now = time.time()
        if busy and now - state["last_report"] >= 5.0:
            state["last_report"] = now
            log(f"still generating: {len(busy)} of {len(components)} ({int(now - started)} s)")
        if busy and now - started < timeout:
            return
        state["done"] = True
        unreal.unregister_slate_post_tick_callback(state["handle"])
        if busy:
            log(f"gave up waiting after {int(timeout)} s; {len(busy)} component(s) still generating", "warning")
        else:
            log(f"generation finished for {len(components)} component(s) in {now - started:.1f} s")
            if on_done is not None:
                on_done()

    state["handle"] = unreal.register_slate_post_tick_callback(tick)
    _watchers.append(state)


def save_level() -> None:
    try:
        if unreal.EditorLevelLibrary.save_current_level():
            log("level saved")
        else:
            log("the level could not be saved (is it read-only or checked in?)", "warning")
    except Exception as exc:
        log(f"saving failed: {exc}", "warning")


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------

def list_components(components: List[Any]) -> None:
    if not components:
        log("no PCG components found")
        return
    for component in components:
        graph = component_graph(component)
        graph_name = graph.get_name() if graph is not None else "(no graph)"
        state = "generating" if is_generating(component) else "idle"
        log(f"{component_name(component)}  graph: {graph_name}  {state}")
    log(f"{len(components)} PCG component(s)")


def run(selected: bool = False, label: str = "", tag: str = "", force: bool = False, cleanup_only: bool = False,
        save: bool = False, list_only: bool = False, timeout: float = 600.0) -> int:
    """Returns the number of components acted on."""
    components = find_components(selected, label, tag)
    if list_only:
        list_components(components)
        return len(components)
    if not components:
        where = "the selected actors" if selected else "the level"
        log(f"no PCG components found on {where}" + (f" matching '{label}'" if label else "")
            + (f" with tag '{tag}'" if tag else ""), "warning")
        return 0

    acted = []
    for component in components:
        name = component_name(component)
        if cleanup_only:
            if cleanup(component):
                log(f"cleaned up {name}")
                acted.append(component)
            continue
        if component_graph(component) is None:
            log(f"skipping {name}: no PCG graph assigned", "warning")
            continue
        if force:
            cleanup(component)
        if generate(component, force):
            log(f"{'regenerating' if force else 'generating'} {name}")
            acted.append(component)

    if cleanup_only:
        log(f"cleaned up {len(acted)} component(s)")
        if save:
            save_level()
        return len(acted)
    if acted:
        watch(acted, on_done=save_level if save else None, timeout=timeout)
    return len(acted)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_pcg.py", description="Generate or clean up PCG components in the open level.")
    parser.add_argument("--selected", action="store_true", help="only the actors selected in the level")
    parser.add_argument("--label", default="", help="only actors whose label contains this text")
    parser.add_argument("--tag", default="", help="only actors with this actor tag")
    parser.add_argument("--force", action="store_true", help="clean up first, then generate again")
    parser.add_argument("--cleanup", action="store_true", help="remove generated content instead of generating")
    parser.add_argument("--save", action="store_true", help="save the level when done")
    parser.add_argument("--list", action="store_true", help="list the PCG components and do nothing else")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for generation (default 600)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    # Unreal's `py file.py a b` passes a and b in sys.argv; drop anything that looks like the script path.
    raw = list(sys.argv[1:] if argv is None else argv)
    raw = [a for a in raw if not a.lower().endswith(".py")]
    try:
        args = parse_args(raw)
    except SystemExit:  # argparse printed help or an error
        return 0
    return run(selected=args.selected, label=args.label, tag=args.tag, force=args.force, cleanup_only=args.cleanup,
               save=args.save, list_only=args.list, timeout=args.timeout)


if __name__ == "__main__":
    main()
