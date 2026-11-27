#!/usr/bin/env python3
"""
Isolated Subprocess Runner for PopcornBox Custom Plugins.
Executes user-supplied Python scrapers out-of-process to protect the main GTK
application process from crashes, segfaults, sys.exit(), hangs, and memory corruption.
"""

import sys
import os
import json
import inspect
import importlib.util
from pathlib import Path

# Add runner directory to sys.path and initialize DoH for censorship bypass
runner_dir = str(Path(__file__).resolve().parent)
if runner_dir not in sys.path:
    sys.path.insert(0, runner_dir)

try:
    import doh
    doh.enable_global_doh()
except Exception:
    pass

if "popcornbox" not in sys.modules:
    try:
        import popcornbox
    except ImportError:
        import types
        pkg = types.ModuleType("popcornbox")
        pkg.__path__ = [runner_dir]
        sys.modules["popcornbox"] = pkg


def _load_module(file_path: str):

    """Load a Python module from file_path in an isolated module namespace."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Plugin file not found: {file_path}")

    module_name = f"popcornbox_isolated_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if not spec or not spec.loader:
        raise ImportError(f"Could not load module spec for {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_scraper_classes(module, target_class_name: str = None):
    """Locate scraper classes within the loaded module."""
    candidates = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if not inspect.isclass(attr):
            continue
        if target_class_name and attr_name == target_class_name:
            return [attr]
        # Match standard custom plugin criteria
        if (
            hasattr(attr, "get_streams")
            or getattr(attr, "is_scraper", False)
            or attr_name == "Scraper"
        ):
            candidates.append(attr)
    return candidates


def inspect_plugin(file_path: str) -> list[dict]:
    """Inspects a plugin file and returns metadata for all scraper classes found."""
    module = _load_module(file_path)
    classes = _find_scraper_classes(module)
    results = []
    path = Path(file_path)

    for cls in classes:
        try:
            instance = cls()
            s_id = str(getattr(instance, "id", None) or f"custom.{path.stem}")
            name = getattr(instance, "name", None) or path.stem.replace("_", " ").title()
            version = getattr(instance, "version", "1.0.0")
            description = getattr(instance, "description", "")
            author = getattr(instance, "author", "")
            media_types = list(getattr(instance, "media_types", ["movie", "series"]))
            catalog_sections = list(getattr(instance, "catalog_sections", []))

            results.append({
                "class_name": cls.__name__,
                "id": s_id,
                "name": name,
                "version": version,
                "description": description,
                "author": author,
                "media_types": media_types,
                "catalog_sections": catalog_sections,
            })
        except Exception as e:
            sys.stderr.write(f"Error instantiating {cls.__name__} in {file_path}: {e}\n")

    return results


def run_get_streams(file_path: str, class_name: str, kwargs: dict) -> list[dict]:
    """Instantiates the scraper and calls get_streams(**kwargs)."""
    module = _load_module(file_path)
    classes = _find_scraper_classes(module, class_name)
    if not classes:
        raise ValueError(f"No scraper class found in {file_path}")

    scraper_cls = classes[0]
    instance = scraper_cls()

    if not hasattr(instance, "get_streams"):
        raise AttributeError(f"{scraper_cls.__name__} has no get_streams method")

    sig = inspect.signature(instance.get_streams)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return instance.get_streams(**kwargs) or []

    bound_kwargs = {k: v for k, v in kwargs.items() if k in params}

    # Fallback to positional imdb_id if no keyword matches but parameters expected
    if not bound_kwargs and len(params) >= 1:
        imdb_id = kwargs.get("imdb_id")
        return instance.get_streams(imdb_id) or []

    return instance.get_streams(**bound_kwargs) or []


def run_get_catalog_items(file_path: str, class_name: str, kwargs: dict) -> list[dict]:
    """Instantiates the scraper and calls get_catalog_items(**kwargs)."""
    module = _load_module(file_path)
    classes = _find_scraper_classes(module, class_name)
    if not classes:
        raise ValueError(f"No scraper class found in {file_path}")

    scraper_cls = classes[0]
    instance = scraper_cls()

    if not hasattr(instance, "get_catalog_items"):
        return []

    sig = inspect.signature(instance.get_catalog_items)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return instance.get_catalog_items(**kwargs) or []

    bound_kwargs = {k: v for k, v in kwargs.items() if k in params}

    if not bound_kwargs and len(params) >= 1:
        cat_id = kwargs.get("catalog_id")
        return instance.get_catalog_items(cat_id) or []

    return instance.get_catalog_items(**bound_kwargs) or []


def main():
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            sys.stderr.write("Empty JSON payload on stdin\n")
            sys.exit(1)

        payload = json.loads(raw_input)
        action = payload.get("action")
        file_path = payload.get("file_path")
        class_name = payload.get("class_name")
        kwargs = payload.get("kwargs", {})

        if not file_path:
            sys.stderr.write("Missing 'file_path' in payload\n")
            sys.exit(1)

        if action == "inspect":
            data = inspect_plugin(file_path)
        elif action == "get_streams":
            data = run_get_streams(file_path, class_name, kwargs)
        elif action == "get_catalog_items":
            data = run_get_catalog_items(file_path, class_name, kwargs)
        else:
            sys.stderr.write(f"Unknown action: {action}\n")
            sys.exit(1)

        # Output JSON result on stdout
        json.dump(data, sys.stdout)
        sys.stdout.flush()
        sys.exit(0)

    except Exception as e:
        sys.stderr.write(f"Runner execution failed: {e}\n")
        json.dump({"error": str(e), "success": False}, sys.stdout)
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
