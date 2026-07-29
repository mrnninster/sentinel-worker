"""
Bridge to vendored dedicated schedule parsers under ``schedule.library``.

Self-contained: parsers live in this project (copied from WallFly), along with
``utils.scrape_html``, ``utils.format_time``, etc. No WallFly repo required.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


async def parse_with_dedicated_scraper(
    url: str,
    schedule_type: str,
    timezone: str,
    agenda_url: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Dispatch to ``schedule.schedule_scraper.parse_schedule``."""
    from schedule.schedule_scraper import parse_schedule

    log.info(
        "Dedicated parser schedule_type=%s url=%s",
        schedule_type,
        url,
    )
    result = await parse_schedule(
        url=url,
        schedule_type=schedule_type,
        timezone=timezone,
        agenda_url=agenda_url,
        retry_if_no_meetings=False,
    )
    if isinstance(result, tuple):
        meetings = result[0]
    else:
        meetings = result
    return list(meetings or [])


def list_schedule_types() -> list[str]:
    """Return public parser method names from ``schedule.library`` modules."""
    import importlib
    import inspect
    import pkgutil
    from pathlib import Path

    library_path = Path(__file__).resolve().parents[1] / "schedule" / "library"
    types: list[str] = []
    for module_info in pkgutil.iter_modules([str(library_path)]):
        name = module_info.name
        if name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"schedule.library.{name}")
        except Exception:
            continue
        class_name = name.capitalize()
        cls = getattr(module, class_name, None)
        if cls is None:
            continue
        try:
            instance = cls()
        except Exception:
            continue
        for method_name, _ in inspect.getmembers(instance, predicate=inspect.ismethod):
            if method_name.startswith("_"):
                continue
            if method_name == name or method_name.startswith(name) or method_name.startswith("unique_"):
                # Prefer methods that look like schedule_type names
                if method_name.endswith("_table") or method_name.startswith("unique_"):
                    types.append(method_name)
                elif method_name == f"{name}_table":
                    types.append(method_name)
        # Also pick any public callable whose name contains the module hint
        for method_name, method in inspect.getmembers(instance, predicate=callable):
            if method_name.startswith("_"):
                continue
            if method_name.endswith("_table") or method_name.startswith("unique_"):
                if method_name not in types:
                    types.append(method_name)
    return sorted(set(types))
