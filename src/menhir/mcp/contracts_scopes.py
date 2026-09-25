"""Tool tenancy-scope declarations and the startup metadata validators."""

from __future__ import annotations

import inspect
from collections.abc import Mapping


class ToolScope:
    """How a tool relates to tenant data, declared rather than inferred (CF-33).

    The namespace pin used to reach a tool only when `inspect.signature(self.endpoint)` happened
    to contain `namespace`. That is not a policy applied to callers; it is a policy applied to
    whichever endpoints happen to name a parameter. Adding a tool silently removed it from the
    pin's reach with no error and no log line, which is why this cluster has been patched
    per-site four times -- CF-16, CF-157, CF-64, CF-30 -- each correct, each leaving the next
    hole intact, and CF-30's own remediation finding a fifth inside the surface it had just fixed.

    Declaring the scope does not by itself close a hole. What it does is make an unconsidered
    tool a STARTUP FAILURE instead of a silent gap, and reduce "41 tools that are global by
    accident" to a list of roughly nine that a human can audit in a minute.
    """

    #: Takes a `namespace` argument; the pin is injected into it.
    NAMESPACED = "namespaced"
    #: Addressed by uuid. The pin cannot be injected as an argument, so tenancy must be checked
    #: at load -- the two-lookup pattern CF-64 established: refuse only when the object exists
    #: AND belongs to another silo, so genuinely-absent objects still report "not found".
    OBJECT = "object"
    #: Operational state that is genuinely not tenant-scoped (scheduler control, client admin).
    #: Kept deliberately small and reviewable.
    GLOBAL = "global"

    ALL = frozenset({NAMESPACED, OBJECT, GLOBAL})


_TIER_RANK: dict[str, int] = {"readonly": 0, "agent": 1, "operator": 2}


def _tier_allows(current: str, required: str) -> bool:
    """Return True when *current* tier satisfies the *required* tier."""
    if current not in _TIER_RANK or required not in _TIER_RANK:
        return False
    return _TIER_RANK[current] >= _TIER_RANK[required]


def _declares_object_key(params: "Mapping[str, inspect.Parameter]") -> bool:
    """Whether an endpoint is actually addressed by an object identifier.

    OBJECT means "the pin cannot be injected as an argument because the caller names a specific
    object". A tool declaring OBJECT while naming no object is not making that claim -- it is
    the third row of CF-33's census, where genuinely-global tools and tenant-scoped tools that
    simply never got a `namespace` argument look identical from outside. Nine tools sat there,
    and CF-216, CF-217 and the four conflict tools all came out of it.

    The original check caught NAMESPACED-without-`namespace` and GLOBAL-with-`namespace` but not
    this, so the one declaration that meant "unexamined" was the one that stayed silent.
    """
    return any(
        "uuid" in name or name == "id" or name.endswith("_id")
        for name in params
        if name != "self"
    )


def assert_tool_scopes_declared(tool_classes: "list[type] | tuple[type, ...]") -> None:
    """Refuse to start when any tool has not declared its tenancy scope (CF-33).

    This is the load-bearing half of the ToolScope work. The enum alone documents; this is what
    makes an omission impossible to ship. Every finding in this cluster is an instance of one
    thing -- a tool was added and nobody decided how it relates to tenant data -- and that was
    invisible precisely because the consequence was silence.

    Also verifies the declaration matches the signature, because a wrong declaration is worse
    than none: a tool marked NAMESPACED whose endpoint has no `namespace` parameter would read
    as pinned in the audit list while the pin cannot actually reach it.

    Raises at import/registration time rather than logging, so a mistake stops a deploy instead
    of reaching production as a quiet gap.
    """

    undeclared: list[str] = []
    invalid: list[str] = []
    mismatched: list[str] = []

    for tool_cls in tool_classes:
        name = getattr(tool_cls, "name", tool_cls.__name__)
        scope = getattr(tool_cls, "scope", None)
        if scope is None:
            undeclared.append(name)
            continue
        if scope not in ToolScope.ALL:
            invalid.append(f"{name}={scope!r}")
            continue
        params = inspect.signature(tool_cls.endpoint).parameters
        declares_namespace = "namespace" in params
        if scope == ToolScope.NAMESPACED and not declares_namespace:
            mismatched.append(f"{name}: declared NAMESPACED but the endpoint takes no `namespace`")
        elif scope == ToolScope.GLOBAL and declares_namespace:
            mismatched.append(f"{name}: declared GLOBAL but the endpoint takes a `namespace`")
        elif scope == ToolScope.OBJECT and not _declares_object_key(params):
            mismatched.append(
                f"{name}: declared OBJECT but the endpoint takes no object identifier "
                "-- OBJECT means addressed by uuid, so a tool with neither a `namespace` nor "
                "an id is either tenant-scoped and missing its argument, or genuinely GLOBAL"
            )

    problems: list[str] = []
    if undeclared:
        problems.append(
            "tools with no `scope` declared: "
            + ", ".join(sorted(undeclared))
            + " -- set scope to one of "
            + ", ".join(sorted(ToolScope.ALL))
            + " (see ToolScope; NAMESPACED and OBJECT are tenant-scoped, GLOBAL is not)"
        )
    if invalid:
        problems.append("tools with an unrecognized `scope`: " + ", ".join(sorted(invalid)))
    if mismatched:
        problems.append("tools whose `scope` contradicts their signature: " + "; ".join(sorted(mismatched)))

    if problems:
        raise RuntimeError("MCP tool scope declarations are incomplete. " + " | ".join(problems))


#: Mapping from required_tier to the exact OAuth scope set a tool must declare. The
#: metadata contract keeps tier and scope coherent so the advertised securitySchemes
#: cannot drift away from what invocation authorization actually enforces.
_TIER_OAUTH_SCOPES: dict[str, tuple[str, ...]] = {
    "readonly": ("menhir:read",),
    "agent": ("menhir:write",),
    "operator": ("menhir:admin",),
}

_SAFETY_HINT_FIELDS: tuple[str, ...] = ("read_only_hint", "destructive_hint", "open_world_hint")


def validate_tool_metadata(tool_classes: "list[type] | tuple[type, ...]") -> None:
    """Refuse to start when any tool has incomplete or incoherent ChatGPT metadata.

    Complements :func:`assert_tool_scopes_declared`: tenancy is checked there, the
    client-facing contract here. Every field below reaches the model choosing a tool
    (title, description, safety hints) or the connector's authorization layer
    (oauth_scopes), so an omission is a silent downgrade exactly like an undeclared
    scope was -- and gets the same treatment: a loud startup failure.

    Raises RuntimeError aggregating every problem across every tool rather than
    failing on the first one, so one bad tool does not hide the rest.
    """

    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    scope_mismatches: list[str] = []

    for tool_cls in tool_classes:
        name = getattr(tool_cls, "name", tool_cls.__name__)

        for field in ("title", "description"):
            value = getattr(tool_cls, field, None)
            if not isinstance(value, str) or not value.strip():
                missing_fields.append(f"{name}.{field}")

        for field in _SAFETY_HINT_FIELDS:
            value = getattr(tool_cls, field, None)
            if not isinstance(value, bool):
                invalid_fields.append(f"{name}.{field}={value!r} (must be bool)")

        scopes = getattr(tool_cls, "oauth_scopes", None)
        if not isinstance(scopes, tuple) or not scopes or not all(isinstance(s, str) and s for s in scopes):
            invalid_fields.append(f"{name}.oauth_scopes={scopes!r} (must be a non-empty tuple of strings)")
            continue

        required_tier = getattr(tool_cls, "required_tier", None)
        expected_scopes = _TIER_OAUTH_SCOPES.get(required_tier)
        if expected_scopes is None:
            invalid_fields.append(
                f"{name}.required_tier={required_tier!r} (must be one of "
                f"{sorted(_TIER_OAUTH_SCOPES)})"
            )
        elif tuple(scopes) != expected_scopes:
            scope_mismatches.append(
                f"{name}: required_tier={required_tier!r} requires oauth_scopes="
                f"{list(expected_scopes)} but declares {list(scopes)}"
            )

    problems: list[str] = []
    if missing_fields:
        problems.append("tools missing required text metadata: " + ", ".join(sorted(set(missing_fields))))
    if invalid_fields:
        problems.append("tools with invalid metadata fields: " + "; ".join(sorted(invalid_fields)))
    if scope_mismatches:
        problems.append("tools whose oauth_scopes contradict their required_tier: " + "; ".join(scope_mismatches))

    if problems:
        raise RuntimeError("MCP tool metadata contract violated. " + " | ".join(problems))
