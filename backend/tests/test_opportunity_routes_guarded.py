"""The filter lives in application code, so a test asserts nobody forgets it.

This is deliberately structural rather than behavioural: a behavioural test
covers the routes that exist today, and the failure mode being guarded
against is a route added next month.
"""

import ast
import pathlib

MODULE = pathlib.Path(__file__).parent.parent / "app" / "api" / "opportunities.py"

READ_GUARD = "load_visible_opportunity"
EDIT_GUARD = "load_editable_opportunity"

# Routes that legitimately do not load a single opportunity by id, or that
# deliberately bypass `can_edit`: claiming an UNASSIGNED job order is exactly
# the case `can_edit` refuses, so `claim_opportunity` cannot go through the
# edit guard.
EXEMPT = {"list_opportunities", "claim_opportunity"}


def _routes() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(MODULE.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr in {
                "get", "post", "put", "patch", "delete"
            }:
                out.append(node)
    return out


def _calls(node: ast.AsyncFunctionDef) -> set[str]:
    return {
        c.func.id
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }


def _takes_opportunity_id(node: ast.AsyncFunctionDef) -> bool:
    return any(a.arg == "opportunity_id" for a in node.args.args)


def test_every_by_id_route_loads_through_the_guard() -> None:
    offenders = [
        node.name
        for node in _routes()
        if _takes_opportunity_id(node)
        and node.name not in EXEMPT
        and not ({READ_GUARD, EDIT_GUARD} & _calls(node))
    ]
    assert offenders == [], (
        f"These routes read an opportunity by id without the visibility guard: {offenders}"
    )


def test_every_mutating_by_id_route_uses_the_edit_guard() -> None:
    mutating = []
    for node in _routes():
        if not _takes_opportunity_id(node) or node.name in EXEMPT:
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr in {
                "post", "put", "patch", "delete"
            }:
                mutating.append(node)
    offenders = [n.name for n in mutating if EDIT_GUARD not in _calls(n)]
    assert offenders == [], (
        f"These routes change an opportunity without checking edit rights: {offenders}"
    )


def test_list_filters_by_the_predicate() -> None:
    source = MODULE.read_text()
    start = source.index("async def list_opportunities")
    end = source.index("\n@router.", start)
    assert "visible_opportunities(" in source[start:end], (
        "list_opportunities does not apply the visibility predicate"
    )