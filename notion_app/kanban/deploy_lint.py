"""Static write-time lint for the Deploy* Kanban properties, same pattern
and same reason as :mod:`check_hint_lint`: a card field that looks right but
is shaped wrong becomes invisible until ``kanban_check_deployed`` (the
agents-platform-runners tool that reads these fields) silently classifies
the card as ``invalid`` and skips it forever. Catching the shape at write
time — when a human/agent is still looking at the card — beats discovering
it a sweep cycle later with no one watching.

Scope is deliberately narrow: this is NOT a check that the component or
version actually exists (the checker's live adapters do that) — only that
the *shape* the checker's adapter registry depends on to even dispatch is
well-formed. See the architect decision on card
4165bf3b-9510-8162-89d0-dca46ba66481 for the DeployComponent namespace and
the semver/SHA identity rules this mirrors.
"""
from __future__ import annotations

import re

_COMPONENT_CLASSES = ("app:", "service:")
_COMPONENT_SCALARS = ("core", "remote-host")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")

_SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_PLACEHOLDER_VALUES = {"latest", "tbd", "todo", "unknown", "n/a", "na", "none", "?"}


def lint_deploy_component(value: str) -> list[str]:
    """Reasons this ``DeployComponent`` risks landing on an adapter that
    can never dispatch it, or ``[]`` if the shape is fine.

    Valid shapes (verbatim from the architect decision): ``app:<slug>``,
    ``service:<name>``, ``core``, ``remote-host`` — exactly one component,
    never a list (a comma/space/semicolon-separated value defeats the
    one-component-per-card cardinality rule the whole schema depends on).
    """
    text = (value or "").strip()
    if not text:
        return ["DeployComponent is empty — kanban_check_deployed cannot pick an "
                "adapter and will classify this card invalid forever"]
    if re.search(r"[,;]|\s", text):
        return [f"DeployComponent {text!r} looks like a list (contains a separator/space) — "
                "only ONE component per card; split multi-component changes into one card "
                "per component grouped by TargetSlug instead"]
    if text in _COMPONENT_SCALARS:
        return []
    for prefix in _COMPONENT_CLASSES:
        if text.startswith(prefix):
            name = text[len(prefix):]
            if not name:
                return [f"DeployComponent {text!r} has no slug/name after '{prefix}'"]
            if not _SLUG_RE.match(name):
                return [f"DeployComponent {text!r} — {name!r} after '{prefix}' isn't a "
                        "lowercase-alnum-hyphen slug, so the live adapter may fail to "
                        "resolve it against /api/apps or the service registry"]
            return []
    return [f"DeployComponent {text!r} doesn't match any known class "
            f"(app:<slug> | service:<name> | core | remote-host) — "
            "kanban_check_deployed classifies unknown classes 'invalid' and skips the card"]


def lint_deploy_to_version(value: str) -> list[str]:
    """Reasons this ``DeployToVersion`` risks being unverifiable, or ``[]``.

    Must be an exact observable identity — a semver tag (``vX.Y.Z``) or a
    git commit SHA (7-40 hex chars) — never a placeholder like 'latest',
    which is not a fixed target the checker's >= comparison can evaluate.
    """
    text = (value or "").strip()
    if not text:
        return ["DeployToVersion is empty — required for eligibility; "
                "kanban_check_deployed treats an empty value as invalid, not pending"]
    if text.lower() in _PLACEHOLDER_VALUES:
        return [f"DeployToVersion {text!r} is a placeholder, not a fixed target — the "
                "checker needs an exact semver tag or commit SHA to compare against"]
    if re.search(r"[,;]|\s", text):
        return [f"DeployToVersion {text!r} looks like a list — exactly one target version "
                "per card (components version independently; split the card if that's wrong)"]
    if _SEMVER_RE.match(text) or _SHA_RE.match(text):
        return []
    return [f"DeployToVersion {text!r} is neither a semver tag (vX.Y.Z) nor a commit SHA "
            "(7-40 hex chars) — the checker's comparison will classify observations against "
            "it as 'unknown' rather than deployed/pending"]
