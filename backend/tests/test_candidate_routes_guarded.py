"""The filter lives in application code, so a test asserts nobody forgets it.

This is deliberately structural rather than behavioural: a behavioural test
covers the routes that exist today, and the failure mode being guarded
against is a route added next month.

**This file is a deliberate copy of `test_opportunity_routes_guarded.py`, not
a shared helper.** The transitive `_reachable` walk and the `_queries_the_
model` verb detection are the subtle parts, and they are what caught two real
leaks. Two independent copies cannot both be disabled by one bug in a shared
walk, and a candidate is not a job order: the two guards drift apart on
purpose (there is no mailbox term in `visible_candidates`, and an unowned
candidate is visible but not editable). Factoring them together would couple
two rules that are allowed to diverge.

The guard follows the model rather than the filename: every module under
`app/api/` that touches `Candidate` is in scope, because a candidate is read
by id from any module that cares to import it.

Exemptions are per-module (`{module: {function: reason}}`), never a flat set
of names. A flat set is a smaller version of the same bug: an exemption
written for `list_candidates` would silently excuse a `list_candidates`
somebody later adds in another file.
"""

import ast
import pathlib

import pytest

API_DIR = pathlib.Path(__file__).parent.parent / "app" / "api"

READ_GUARD = "load_visible_candidate"
EDIT_GUARD = "load_editable_candidate"
EDIT_CHECK = "can_edit_candidate"
PREDICATE = "visible_candidates"

# A module is in scope if it names any of these. `Candidate` catches the
# module that reads the row; the guard names catch a module that has stopped
# importing the model but still reads a candidate through the service. Either
# alone would let a file drop out of this test through a refactor that changes
# nothing about who can see what.
IN_SCOPE_NAMES = {"Candidate", READ_GUARD, EDIT_GUARD, PREDICATE}

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
    "candidates.py": {
        "list_candidates": (
            "Lists rather than loading one by id; it applies "
            "`visible_candidates` directly, which "
            "`test_list_filters_by_the_predicate` asserts."
        ),
        "create_candidate": (
            "Builds a candidate rather than reaching for somebody else's. Its "
            "collision check queries the WHOLE tenant on purpose — the unique "
            "index spans the tenant, so a visibility-filtered check would let "
            "the insert fail on a constraint instead of returning the 409."
        ),
        "delete_candidate": (
            "Goes through `_require_owner`, which is strictly narrower than "
            "either guard: only an agency owner may delete, whatever the "
            "candidate's own ownership or shares say."
        ),
    },
    "candidate_shares.py": {
        "request_candidate_access": (
            "The one route deliberately reachable for an INVISIBLE candidate. "
            "That is the entire point of the access-request path, and it "
            "returns nothing whatsoever about the row it names."
        ),
    },
}


# Exempt from the EDIT assertion ONLY, and still held to the READ one.
#
# A separate dict rather than an entry in `EXEMPT`, because `EXEMPT` drops a
# route out of `_in_scope_routes` altogether and so excuses it from both
# assertions. Every route here writes a row that is NOT the candidate, or
# creates the edit rights it could not already hold; each still has to load
# the candidate through the read guard, and does.
#
# allow-hardcode: prose for a human reading a failure; only the keys match.
EDIT_ONLY_EXEMPT: dict[str, dict[str, str]] = {
    "candidates.py": {
        "log_activity": (
            "The row it writes is a `candidate_activities` row, not the "
            "candidate. A share recipient may record that they opened a "
            "WhatsApp chat: that is a fact about what they did, not an edit."
        ),
    },
    # allow-hardcode: prose for a human reading a failure, not logic — only
    # the keys are matched on.
    "candidate_whatsapp.py": {
        "whatsapp_send": (
            "Writes a `candidate_activities` row, the same rule as "
            "`log_activity`. Messaging a candidate somebody shared with you "
            "is the point of sharing one; it changes nothing about the "
            "candidate record."
        ),
    },
    "sourcing.py": {
        "record_submission": (
            "The row it writes is a `candidate_submissions` row, not the "
            "candidate. A share recipient may put a shared candidate in front "
            "of a client: that is visibility, not edit rights."
        ),
        "withdraw_submission": (
            "The other half of `record_submission`, and narrower — it deletes "
            "only a `candidate_submissions` row. Whoever could create it must "
            "be able to undo it, or a misclick is permanent."
        ),
    },
    "candidate_ownership.py": {
        "claim_candidate": (
            "Claiming an UNOWNED candidate is exactly the case "
            "`can_edit_candidate` refuses, so it cannot go through the edit "
            "guard — claiming is the act that creates edit rights."
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
    assert found, f"no API module under {API_DIR} mentions a candidate"
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
    rule as squarely as an inline call does.
    """
    return set().union(*(_own_calls(f) for f in _reachable(node, defined)))


# The verbs that read or change rows that already exist. `Candidate(...)` on
# its own is a constructor — `create_candidate` builds a candidate rather than
# reaching for somebody else's, and there is nothing yet to be visible.
QUERY_VERBS = {"select", "update", "delete", "get"}


def _queries_the_model(node: FunctionDef, defined: dict[str, FunctionDef]) -> bool:
    """A query anywhere in the route's reach, not just in its own body.

    Inspecting the route body alone was a hole, and a proven one: a route that
    passed the id to a module-level helper and let the helper run
    `select(Candidate)` read a row while this test saw nothing.
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
            if any(isinstance(a, ast.Name) and a.id == "Candidate" for a in call.args):
                return True
    return False


# A candidate id does not have to be called `candidate_id`, and pinning the
# check to that one spelling made a route invisible by renaming a parameter.
CANDIDATE_PARAM_HINTS = ("candidate",)


def _takes_a_candidate_param(node: FunctionDef) -> bool:
    args = node.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        lowered = arg.arg.lower()
        if any(hint in lowered for hint in CANDIDATE_PARAM_HINTS):
            return True
    return False


def _reads_a_candidate_by_id(node: FunctionDef, defined: dict[str, FunctionDef]) -> bool:
    """Two ways in, and the second is the one that was missed.

    A parameter naming a candidate is the obvious way. The other is a *query*
    parameter that fetches a `Candidate` under RLS alone. So a query against
    the model counts too, whatever the parameter is called — and it counts
    whether the route runs it or delegates it.
    """
    return _takes_a_candidate_param(node) or _queries_the_model(node, defined)


def _exempt(module: pathlib.Path, node: ast.AsyncFunctionDef) -> bool:
    return node.name in EXEMPT.get(module.name, {})


def _in_scope_routes() -> list[tuple[pathlib.Path, ast.AsyncFunctionDef, dict[str, FunctionDef]]]:
    triples = []
    for module in _modules():
        tree = ast.parse(module.read_text())
        defined = _module_functions(tree)
        for node in _routes(tree):
            if _reads_a_candidate_by_id(node, defined) and not _exempt(module, node):
                triples.append((module, node, defined))
    return triples


def test_every_by_id_route_loads_through_the_guard() -> None:
    offenders = [
        f"{module.name}::{node.name}"
        for module, node, defined in _in_scope_routes()
        if not ({READ_GUARD, EDIT_GUARD} & _calls(node, defined))
    ]
    assert offenders == [], (
        f"These routes read a candidate by id without the visibility guard: {offenders}"
    )


def test_every_mutating_by_id_route_uses_the_edit_guard() -> None:
    offenders = [
        f"{module.name}::{node.name}"
        for module, node, defined in _in_scope_routes()
        if MUTATING_METHODS & _decorator_methods(node)
        and node.name not in EDIT_ONLY_EXEMPT.get(module.name, {})
        # `can_edit_candidate` counts: a module may load through the read
        # guard and then apply `can_edit_candidate` itself where the rule is
        # wider than editing the candidate. That is a deliberate, checked
        # decision, not a missing one.
        and not ({EDIT_GUARD, EDIT_CHECK} & _calls(node, defined))
    ]
    assert offenders == [], (
        f"These routes change a candidate without checking edit rights: {offenders}"
    )


@pytest.mark.xfail(
    reason="candidate_shares.py and candidate_ownership.py land in Tasks 8 and 11",
    strict=True,
)
def test_the_guard_covers_more_than_one_module() -> None:
    """The regression this file exists to prevent a second time.

    Reach is the whole point, so it is asserted rather than assumed: a change
    that quietly narrowed `_modules()` back to a single file would otherwise
    leave every assertion above passing.
    """
    modules = {m.name for m in _modules()}
    assert {"candidates.py", "candidate_shares.py", "candidate_ownership.py"} <= modules, modules
    assert len({m.name for m, _, _ in _in_scope_routes()}) > 1


def test_list_filters_by_the_predicate() -> None:
    source = (API_DIR / "candidates.py").read_text()
    start = source.index("async def list_candidates")
    end = source.index("\n@router.", start)
    assert f"{PREDICATE}(" in source[start:end], (
        "list_candidates does not apply the visibility predicate"
    )


@pytest.mark.xfail(
    reason="candidate_shares.py and candidate_ownership.py land in Tasks 8 and 11",
    strict=True,
)
def test_every_exemption_names_a_route_that_exists() -> None:
    """An exemption for a route that has gone is a hole waiting for a name.

    Delete the route, leave the exemption, and the next function to take that
    name in that file is excused before it is written.
    """
    stale = []
    both = [*EXEMPT.items(), *EDIT_ONLY_EXEMPT.items()]
    for module_name, reasons in both:
        module = API_DIR / module_name
        assert module.exists(), module_name
        present = {n.name for n in _routes(ast.parse(module.read_text()))}
        for name, reason in reasons.items():
            assert reason.strip(), f"{module_name}::{name} is exempt with no reason"
            if name not in present:
                stale.append(f"{module_name}::{name}")
    assert stale == [], f"Exemptions for routes that no longer exist: {stale}"
