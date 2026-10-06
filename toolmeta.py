"""MCP tool metadata: the display title and the four behaviour hints.

Must stay free of imports: registry.py and watcher.py both import it at
module level.

Hints follow the MCP 2025-06-18 ToolAnnotations definitions and describe the
worst case of a real call; dry_run defaulting to true does not make a tool
non-destructive. All four are always set explicitly.

The hints are advisory. The plain-HTTP route is gated by MUTATING_TOOLS in
http_tools.py, an explicit list; tests/test_annotations.py checks that the
two agree.
"""


def meta(title, read_only, destructive, idempotent, open_world):
    """Fields to merge into a tool entry: top-level title plus annotations."""
    return {
        "title": title,
        "annotations": {
            "title": title,
            "readOnlyHint": bool(read_only),
            "destructiveHint": bool(destructive),
            "idempotentHint": bool(idempotent),
            "openWorldHint": bool(open_world),
        },
    }
