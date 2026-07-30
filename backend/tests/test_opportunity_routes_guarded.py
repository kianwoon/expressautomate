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


def _calls(node: ast.AsyncFunctionDef) -> set[str]:
    return {
        c.func.id
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }


# The verbs that read or change rows that already exist. `Opportunity(...)`
# on its own is a constructor — `create_opportunity` builds a job order rather
# than reaching for somebody else's, and there is nothing yet to be visible.
QUERY_VERBS = {"select", "update", "delete", "get"}


def _queries_the_model(node: ast.AsyncFunctionDef) -> bool:
    for call in ast.walk(node):
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


def _reads_a_job_order_by_id(node: ast.AsyncFunctionDef) -> bool:
    """Two ways in, and the second is the one that was missed.

    A path parameter called `opportunity_id` is the obvious way. The leak in
    `candidates.py` arrived by the other: a *query* parameter
    (`?eligible_for=`) that fetched an `Opportunity` under RLS alone. So a
    query against the model counts too, whatever the parameter is called.
    """
    if any(a.arg == "opportunity_id" for a in node.args.args):
        return True
    return _queries_the_model(node)


def _exempt(module: pathlib.Path, node: ast.AsyncFunctionDef) -> bool:
    return node.name in EXEMPT.get(module.name, {})


def _in_scope_routes() -> list[tuple[pathlib.Path, ast.AsyncFunctionDef]]:
    pairs = []
    for module in _modules():
        tree = ast.parse(module.read_text())
        for node in _routes(tree):
            if _reads_a_job_order_by_id(node) and not _exempt(module, node):
                pairs.append((module, node))
    return pairs


def test_every_by_id_route_loads_through_the_guard() -> None:
    offenders = [
        f"{module.name}::{node.name}"
        for module, node in _in_scope_routes()
        if not ({READ_GUARD, EDIT_GUARD} & _calls(node))
    ]
    assert offenders == [], (
        f"These routes read an opportunity by id without the visibility guard: {offenders}"
    )


def test_every_mutating_by_id_route_uses_the_edit_guard() -> None:
    offenders = [
        f"{module.name}::{node.name}"
        for module, node in _in_scope_routes()
        if MUTATING_METHODS & _decorator_methods(node)
        # `can_edit` counts: `opportunity_shares.py` loads through the read
        # guard and then applies `can_edit` itself, because who may withdraw a
        # share is wider than who may edit the job order (the recipient may
        # leave). That is a deliberate, checked decision, not a missing one.
        and not ({EDIT_GUARD, EDIT_CHECK} & _calls(node))
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
    assert len({m.name for m, _ in _in_scope_routes()}) > 1


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
