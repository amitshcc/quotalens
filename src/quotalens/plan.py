"""Which plan the account is on, read from ``/api/bootstrap``.

**Informational only.** Nothing in the dashboard branches on the plan: the
meters, the budget, Weeks and the subcap check stay shaped by the readings,
because a plan label can be wrong or renamed and a reading cannot. The plan is
shown next to the mark, on the About page and in ``/api/health``.

The usage payload carries no plan field. Bootstrap does, per organization
membership (verified 2026-10-02 on a Max 5x account)::

    account.memberships[].organization.capabilities     ['chat', 'claude_max']
    account.memberships[].organization.rate_limit_tier  'default_claude_max_5x'
    account.memberships[].organization.billing_type     'stripe_subscription'

Only those three fields are kept, for the active organization (matched by
``organization.uuid``). Never names, emails, ids or the rest of the payload, and
nothing from bootstrap is written to the ``sample`` table.

**Cadence.** Fetched on the first successful poll after start, then at most once
per 24 hours, remembered in the database. A failure keeps the last good plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

REFRESH_INTERVAL_S = 86400
MAX_FIELD_CHARS = 40  # a tier string is shown raw when unrecognised; bound it
MAX_CAPABILITIES = 32

FREE = "Free"
# A paid claude.ai plan we can name. Checked in this order, so an org that ever
# carried two of them reads as the larger.
_NAMED_CAPABILITIES = (
    ("claude_enterprise", "Enterprise"),
    ("claude_team", "Team"),
    ("claude_max", "Max"),
    ("claude_pro", "Pro"),
)
_CHAT = "chat"  # every claude.ai org has it; an API-only org does not
NAMED_LABELS = frozenset({"Max 20x", "Max 5x", "Max", "Pro", "Team", "Enterprise", FREE})


@dataclass(frozen=True)
class Plan:
    label: str | None
    tier: str | None
    capabilities: tuple[str, ...]
    billing_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The ``/api/health`` shape: ``{label, tier, capabilities}``."""
        return {"label": self.label, "tier": self.tier, "capabilities": list(self.capabilities)}

    def capabilities_json(self) -> str:
        return json.dumps(list(self.capabilities))


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:MAX_FIELD_CHARS]


def _capabilities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    kept = [text for text in (_text(item) for item in value) if text is not None]
    return tuple(kept[:MAX_CAPABILITIES])


def named(label: str | None) -> str | None:
    """The label if it is a plan we can name, else ``None``: what the header shows.

    A raw tier string is a fact about the account but not a name a reader knows,
    so it stays on the About page and in ``/api/health``.
    """
    return label if label in NAMED_LABELS else None


def label_for(capabilities: tuple[str, ...] | list[str], tier: str | None) -> str | None:
    """A display name. ``None`` when there is nothing to say.

    ``claude_max`` with a tier containing ``20x`` / ``5x`` is "Max 20x" / "Max 5x",
    otherwise "Max"; ``claude_pro`` "Pro"; ``claude_team`` / ``claude_enterprise``
    "Team" / "Enterprise". A claude.ai org (``chat``) with none of those is "Free"
    -- unless it carries a ``claude_*`` capability we do not know, which is a plan
    we cannot name, so the raw tier string is shown instead. An org without
    ``chat`` (an API org) shows its raw tier, or nothing.
    """
    caps = set(capabilities)
    tier_text = tier or ""
    for capability, name in _NAMED_CAPABILITIES:
        if capability not in caps:
            continue
        if capability == "claude_max":
            if "20x" in tier_text:
                return "Max 20x"
            if "5x" in tier_text:
                return "Max 5x"
        return name
    unknown_plan = any(cap.startswith("claude_") for cap in caps)
    if _CHAT in caps and not unknown_plan:
        return FREE
    return _text(tier)


def _memberships(data: Any) -> list[dict[str, Any]]:
    account = data.get("account") if isinstance(data, dict) else None
    memberships = account.get("memberships") if isinstance(account, dict) else None
    if not isinstance(memberships, list):
        return []
    return [
        m for m in memberships if isinstance(m, dict) and isinstance(m.get("organization"), dict)
    ]


def _active_org_id(data: Any, org_id: str | None) -> str | None:
    if org_id:
        return org_id
    account = data.get("account") if isinstance(data, dict) else None
    last = account.get("lastActiveOrgId") if isinstance(account, dict) else None
    return last if isinstance(last, str) and last else None


def from_bootstrap(data: Any, org_id: str | None) -> Plan | None:
    """The active organization's plan, or ``None`` when it cannot be found.

    The membership whose ``organization.uuid`` is ``org_id`` (the org the poller
    reads usage for); without an id, ``account.lastActiveOrgId``; failing that, the
    only membership if there is exactly one. Never guesses between several.
    """
    memberships = _memberships(data)
    wanted = _active_org_id(data, org_id)
    org: dict[str, Any] | None = None
    if wanted is not None:
        for membership in memberships:
            if membership["organization"].get("uuid") == wanted:
                org = membership["organization"]
                break
    elif len(memberships) == 1:
        org = memberships[0]["organization"]
    if org is None:
        return None
    capabilities = _capabilities(org.get("capabilities"))
    tier = _text(org.get("rate_limit_tier"))
    return Plan(
        label=label_for(capabilities, tier),
        tier=tier,
        capabilities=capabilities,
        billing_type=_text(org.get("billing_type")),
    )


def from_row(
    label: str | None,
    tier: str | None,
    capabilities: str | None,
    billing_type: str | None = None,
) -> Plan | None:
    """A stored row back to a :class:`Plan`; ``None`` when nothing was ever found."""
    try:
        caps = json.loads(capabilities) if capabilities else []
    except ValueError:
        caps = []
    plan = Plan(label, tier, _capabilities(caps), billing_type)
    if plan.label is None and plan.tier is None and not plan.capabilities:
        return None
    return plan


class PlanStore(Protocol):
    """What this module needs from the database; ``Store`` satisfies it."""

    def read_plan(self) -> Any: ...


def stored(store: PlanStore) -> Plan | None:
    """The last plan found, or ``None`` before the first fetch or when none was found."""
    row = store.read_plan()
    if row is None:
        return None
    return from_row(row.label, row.tier, row.capabilities, row.billing_type)


def due(checked_ts: int | None, now: int) -> bool:
    return checked_ts is None or now - checked_ts >= REFRESH_INTERVAL_S
