"""The filter lives in application code, so a test asserts nobody forgets it.

This is deliberately structural rather than behavioural: a behavioural test
covers the routes that exist today, and the failure mode being guarded
against is a route added next month.

**It used to guard one file, and that was the bug.** Pinned to
`app/api/opportunities.py`, it watched the module whose author was thinking
about visibility and ignored the ones whose author was not — which is where
two leaks duly appeared, in `sourcing.py` and `candidates.py`, and survived
every per-task review. A job order is read by id from any module that cares
to import it, so the guard now follows the model rather than the filename:
every module under `app/api/` that touches `Opportunity` is in scope.

Exemptions are per-module (`{module: {function: reason}}`), never a flat set
of names. A flat set is a smaller version of the same bug: an exemption
written for `list_opportunities` would silently excuse a `list_opportunities`
somebody later adds in another file.
"""

import ast
import pathlib

API_DIR = pathlib.Path(__file__).parent.parent / "app" / "api"

READ_GUARD = "load_visible_opportunity"
EDIT_GUARD = "load_editable_opportunity"
EDIT_CHECK = "can_edit"
PREDICATE = "visible_opportunities"

# A module is in scope if it names any of these. `Opportunity` catches the
# module that reads the row; the guard names catch a module that has stopped
# importing the model but still reads a job order through the service — the
# state `sourcing.py` is in today. Either alone would let a file drop out of
# this test through a refactor that changes nothing about who can see what.
IN_SCOPE_NAMES = {"Opportunity", READ_GUARD, EDIT_GUARD, PREDICATE}

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
MUTATING_METHODS = {"post", "put", "patch", "delete"}

# {module filename: {route function: why it does not need the guard}}
#
# Written per-module and with a reason each, because "this route is fine" is a
# claim about one route in one file and must not travel.
#
# allow-hardcode: the values are prose for a human reading a failure, not
# logic — only the keys are matched on, and `test_every_exemption_names_a_
# route_that_exists` fails if a key stops naming a real route.
EXEMPT: dict[str, dict[str, str]] = {
    "opportunities.py": {
        "list_opportunities": (
            "Lists rather than loading one by id; it applies "
            "`visible_opportunities` directly, which "
            "`test_list_filters_by_the_predicate` asserts."
        ),
        "claim_opportunity": (
            "Claiming an UNASSIGNED job order is exactly the case `can_edit` "
            "refuses, so it cannot go through the edit guard — claiming is "
            "the act that creates edit rights."
        ),
    },
    "sourcing.py": {
        "start_sourcing": (
            "Exempt from the EDIT assertion only, and it still fails the read "
            "assertion if it drops the read guard. The row it writes is a "
            "`sourcing_runs` row, not the job order. A share recipient may "
            "start a shortlist on work shown to them: that is visibility, "
            "not edit rights."
        ),
    },
    "clients.py": {
        "set_client_assignee": (
            "Reads no job order by id. It moves a whole client's open job "
            "orders in one UPDATE keyed on `client_id`, so there is no id "
            "for the guard to take. Who may reassign a client is a separate, "
            "undecided product question."
        ),
    },
}


def _modules() -> list[pathlib.Path]:
    found = [
        path
        for path in sorted(API_DIR.glob("*.py"))
        if any(name in path.read_text() for name in IN_SCOPE_NAMES)
    ]
    # If this ever empties — a package rename, say — every assertion below
    # would pass over nothing and report success.
    assert found, f"no API module under {API_DIR} mentions a job order"
    return found


def _decorator_methods(node: ast.AsyncFunctionDef) -> set[str]:
    methods = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            methods.add(target.attr)
    return methods


def _routes(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and HTTP_METHODS & _decorator_methods(node)
    ]


FunctionDef = ast.AsyncFunctionDef | ast.FunctionDef


def _own_calls(node: FunctionDef) -> set[str]:
    return {
        c.func.id
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }


def _module_functions(tree: ast.AST) -> dict[str, FunctionDef]:
    """Every function defined at module level, by name.

    Only module level: a nested closure is already inside the body being
    walked, and a method on a class is not what a route calls by bare name.
    """
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def _reachable(node: FunctionDef, defined: dict[str, FunctionDef]) -> list[FunctionDef]:
    """The route plus every same-module helper it can reach, transitively.

    One level would have closed the hole that was found, but not the next one:
    a route calling `_load_context` calling `_fetch` would hide the read again,
    and nothing about a two-hop chain is unusual. The walk is a fixed point
    with a `seen` set, so a recursive or mutually recursive helper terminates.
    """
    seen: set[str] = set()
    closure = [node]
    queue = list(_own_calls(node))
    while queue:
        name = queue.pop()
        if name in seen or name not in defined:
            continue
        seen.add(name)
        helper = defined[name]
        closure.append(helper)
        queue.extend(_own_calls(helper))
    return closure


def _calls(node: FunctionDef, defined: dict[str, FunctionDef]) -> set[str]:
    """Calls made by the route *or* by a helper it delegates to.

    A helper that loads through the guard and hands back the row satisfies the
    rule as squarely as an inline call does — that is how `sourcing.py`'s
    submission routes are written.
    """
    return set().union(*(_own_calls(f) for f in _reachable(node, defined)))


# The verbs that read or change rows that already exist. `Opportunity(...)`
# on its own is a constructor — `create_opportunity` builds a job order rather
# than reaching for somebody else's, and there is nothing yet to be visible.
QUERY_VERBS = {"select", "update", "delete", "get"}


def _queries_the_model(node: FunctionDef, defined: dict[str, FunctionDef]) -> bool:
    """A query anywhere in the route's reach, not just in its own body.

    Inspecting the route body alone was a hole, and a proven one: a route that
    passed the id to a module-level helper and let the helper run
    `select(Opportunity)` read a job order while this test saw nothing —
    exactly the shape of the original `sourcing.py` leak.
    """
    for function in _reachable(node, defined):
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            target = call.func
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else None
            )
            if name not in QUERY_VERBS:
                continue
            if any(isinstance(a, ast.Name) and a.id == "Opportunity" for a in call.args):
                return True
    return False


# A job order id does not have to be called `opportunity_id`, and pinning the
# check to that one spelling made a route invisible by renaming a parameter.
JOB_ORDER_PARAM_HINTS = ("opportunity", "job_order", "joborder", "joa")


def _takes_a_job_order_param(node: FunctionDef) -> bool:
    args = node.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        lowered = arg.arg.lower()
        if any(hint in lowered for hint in JOB_ORDER_PARAM_HINTS):
            return True
    return False


def _reads_a_job_order_by_id(node: FunctionDef, defined: dict[str, FunctionDef]) -> bool:
    """Two ways in, and the second is the one that was missed.

    A parameter naming a job order is the obvious way. The leak in
    `candidates.py` arrived by the other: a *query* parameter
    (`?eligible_for=`) that fetched an `Opportunity` under RLS alone. So a
    query against the model counts too, whatever the parameter is called —
    and it counts whether the route runs it or delegates it.
    """
    return _takes_a_job_order_param(node) or _queries_the_model(node, defined)


def _exempt(module: pathlib.Path, node: ast.AsyncFunctionDef) -> bool:
    return node.name in EXEMPT.get(module.name, {})


def _in_scope_routes() -> list[tuple[pathlib.Path, ast.AsyncFunctionDef, dict[str, FunctionDef]]]:
    triples = []
    for module in _modules():
        tree = ast.parse(module.read_text())
        defined = _module_functions(tree)
        for node in _routes(tree):
            if _reads_a_job_order_by_id(node, defined) and not _exempt(module, node):
                triples.append((module, node, defined))
    return triples


def test_every_by_id_route_loads_through_the_guard() -> None:
    offenders = [
        f"{module.name}::{node.name}"
        for module, node, defined in _in_scope_routes()
        if not ({READ_GUARD, EDIT_GUARD} & _calls(node, defined))
    ]
    assert offenders == [], (
        f"These routes read an opportunity by id without the visibility guard: {offenders}"
    )


def test_every_mutating_by_id_route_uses_the_edit_guard() -> None:
    offenders = [
        f"{module.name}::{node.name}"
        for module, node, defined in _in_scope_routes()
        if MUTATING_METHODS & _decorator_methods(node)
        # `can_edit` counts: `opportunity_shares.py` loads through the read
        # guard and then applies `can_edit` itself, because who may withdraw a
        # share is wider than who may edit the job order (the recipient may
        # leave). That is a deliberate, checked decision, not a missing one.
        and not ({EDIT_GUARD, EDIT_CHECK} & _calls(node, defined))
    ]
    assert offenders == [], (
        f"These routes change an opportunity without checking edit rights: {offenders}"
    )


def test_the_guard_covers_more_than_one_module() -> None:
    """The regression this file exists to prevent a second time.

    Reach is the whole point, so it is asserted rather than assumed: a change
    that quietly narrowed `_modules()` back to a single file would otherwise
    leave every assertion above passing.
    """
    modules = {m.name for m in _modules()}
    assert {"opportunities.py", "sourcing.py", "candidates.py"} <= modules, modules
    assert len({m.name for m, _, _ in _in_scope_routes()}) > 1


def test_list_filters_by_the_predicate() -> None:
    source = (API_DIR / "opportunities.py").read_text()
    start = source.index("async def list_opportunities")
    end = source.index("\n@router.", start)
    assert f"{PREDICATE}(" in source[start:end], (
        "list_opportunities does not apply the visibility predicate"
    )


def test_every_exemption_names_a_route_that_exists() -> None:
    """An exemption for a route that has gone is a hole waiting for a name.

    Delete the route, leave the exemption, and the next function to take that
    name in that file is excused before it is written.
    """
    stale = []
    for module_name, reasons in EXEMPT.items():
        module = API_DIR / module_name
        assert module.exists(), module_name
        present = {n.name for n in _routes(ast.parse(module.read_text()))}
        for name, reason in reasons.items():
            assert reason.strip(), f"{module_name}::{name} is exempt with no reason"
            if name not in present:
                stale.append(f"{module_name}::{name}")
    assert stale == [], f"Exemptions for routes that no longer exist: {stale}"
