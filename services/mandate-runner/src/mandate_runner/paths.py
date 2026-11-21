"""Path patterns from the config, and the pyproject tool-settings comparison.

Patterns are relative to the repo root. `*` and `?` stay inside one path segment;
a `**` segment matches zero or more segments, or everything below when last.
A pattern without wildcards matches exactly that path.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from functools import lru_cache


def _segment(segment: str) -> str:
    out = []
    for ch in segment:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


@lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> re.Pattern[str]:
    parts = pattern.strip("/").split("/")
    out = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
        else:
            out.append(_segment(part) + ("" if last else "/"))
    return re.compile("^" + "".join(out) + "$")


def first_match(paths: Iterable[str], patterns: Iterable[str]) -> str | None:
    """The first changed path that any pattern matches, or None."""
    compiled = [compile_pattern(p) for p in patterns]
    for path in paths:
        if any(rx.match(path) for rx in compiled):
            return path
    return None


def tool_settings_changed(base: bytes | None, head: bytes | None) -> bool:
    """True when the [tool.*] tables differ. Unparseable counts as changed."""

    def tool(content: bytes | None) -> object:
        if content is None:
            return None
        return tomllib.loads(content.decode("utf-8")).get("tool")

    try:
        return tool(base) != tool(head)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return True
