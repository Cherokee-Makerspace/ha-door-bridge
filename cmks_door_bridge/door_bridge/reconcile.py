"""Decide what the door should look like, given Fabman and UniFi state.

Pure functions only: no network, no clock reads (``today`` is passed in), so
every rule here is covered by tests/test_reconcile.py.

Which members the bridge manages: only Fabman members whose metadata links
them to the door — ``cmks_unifi_user_id`` (already on the door) or
``cmks_pin_sealed`` (approved online, waiting for first payment). UniFi users
that aren't linked are never touched, so existing keyfob/PIN users keep
working exactly as they do today.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

PIN_KEY = 'cmks_pin_sealed'
UNIFI_KEY = 'cmks_unifi_user_id'
ERROR_KEY = 'cmks_unifi_error'


@dataclass
class Action:
    kind: str  # 'provision' | 'link' | 'repin' | 'activate' | 'deactivate'
    member: dict
    unifi_user_id: str | None = None
    reason: str = ''


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    waiting: list[tuple[dict, str]] = field(default_factory=list)
    held: bool = False
    hold_reason: str = ''

    def of(self, kind):
        return [a for a in self.actions if a.kind == kind]


def norm_email(e) -> str:
    """Same normalizer as the website: no whitespace/zero-width, lowercase."""
    return re.sub(r'[\s\u200b-\u200d\ufeff]+', '', str(e or '')).lower()


def name(member: dict) -> str:
    return f"{member.get('firstName') or ''} {member.get('lastName') or ''}".strip() or f"member {member['id']}"


def has_active_package(member: dict, today: date) -> bool:
    # With embed=activePackages Fabman returns them under _embedded.memberPackages
    # (seen live 2026-09-26); accept either name. Dates are re-checked below.
    emb = member.get('_embedded') or {}
    for mp in emb.get('memberPackages') or emb.get('activePackages') or []:
        start = mp.get('fromDate')
        end = mp.get('untilDate')
        if start and date.fromisoformat(start[:10]) > today:
            continue
        if end and date.fromisoformat(end[:10]) < today:
            continue
        return True
    return False


def overdue_members(unpaid_invoices: list[dict], today: date, grace_days: int) -> set[int]:
    """Members with an unpaid invoice dated on/before today - grace_days.

    Fabman has no due-date or "overdue" field (verified against the API spec,
    2026-09-25), so the invoice date plus a grace period is the rule. Only
    state == 'unpaid' counts; 'pending'/'processing' mean a payment is in flight.
    """
    cutoff = today - timedelta(days=grace_days)
    out = set()
    for inv in unpaid_invoices:
        if inv.get('state') != 'unpaid':
            continue
        if inv.get('member') is None:
            continue  # not a member's invoice (walk-in sale, guest); can't affect door access
        when = inv.get('date') or inv.get('createdAt')
        if when and date.fromisoformat(when[:10]) <= cutoff:
            out.add(int(inv['member']))
    return out


def good_standing(member: dict, overdue: set[int], today: date) -> tuple[bool, str]:
    if member.get('state') != 'active':
        return False, f"Fabman state is {member.get('state')!r}"
    if not has_active_package(member, today):
        return False, 'no active membership package'
    if int(member['id']) in overdue:
        return False, 'unpaid invoice'
    # Dependents are billed to whoever pays for them (Fabman "paidForBy"), so
    # the payer's unpaid invoice is theirs too. Only matters for dependents
    # given a PIN by hand (team leaders); the form never gives them one.
    payer = member.get('paidForBy')
    if payer and int(payer) in overdue:
        return False, 'the member who pays for them has an unpaid invoice'
    return True, 'active, paid up'


def plan(
    members: list[dict],
    unifi_users: list[dict],
    unpaid_invoices: list[dict],
    paid_member_ids: set[int],
    today: date,
    grace_days: int = 3,
    max_deactivations: int = 3,
) -> Plan:
    """Work out every change needed this run.

    ``paid_member_ids``: members with at least one paid invoice. Only consulted
    for new sign-ups, so a PIN is never put on the door before the member has
    actually paid (approval adds the package — and so "good standing" — before
    the first invoice exists).
    """
    overdue = overdue_members(unpaid_invoices, today, grace_days)
    by_id = {str(u['id']): u for u in unifi_users}
    by_employee = {str(u.get('employee_number')): u for u in unifi_users if u.get('employee_number')}
    by_email = {}
    for u in unifi_users:
        e = norm_email(u.get('user_email') or u.get('email'))
        if e:
            by_email.setdefault(e, []).append(u)
    result = Plan()

    for m in members:
        meta = m.get('metadata') or {}
        uid = meta.get(UNIFI_KEY)
        ok, why = good_standing(m, overdue, today)

        if meta.get(ERROR_KEY):
            # Set when UniFi rejected the PIN. Staff fix it in UniFi, then
            # delete this key; until then leave the user exactly as they are.
            if uid or meta.get(PIN_KEY):
                result.waiting.append((m, f"needs staff: {meta[ERROR_KEY]}"))
            continue

        if not uid and meta.get(PIN_KEY):
            existing = by_employee.get(str(m['id']))
            same_email = by_email.get(norm_email(m.get('emailAddress')), [])
            if existing:
                # A previous run created the UniFi user but died before writing
                # the link back to Fabman. Link it instead of creating a twin.
                result.actions.append(Action('link', m, str(existing['id']), 'UniFi user already exists'))
            elif len(same_email) == 1:
                # Already a door user (added by hand, keyfob, …): link to them;
                # the next run sees the link + sealed PIN and sets the PIN.
                result.actions.append(Action('link', m, str(same_email[0]['id']), 'existing UniFi user with the same email'))
            elif len(same_email) > 1:
                result.waiting.append((m, f'{len(same_email)} UniFi users share this email; link one by hand'))
            elif not ok:
                result.waiting.append((m, why))
            elif int(m['id']) not in paid_member_ids:
                result.waiting.append((m, 'waiting for first payment'))
            else:
                result.actions.append(Action('provision', m, None, 'approved and paid'))
            continue

        if not uid:
            continue
        user = by_id.get(str(uid))
        if user is None:
            result.waiting.append((m, f'linked UniFi user {uid} not found — deleted in UniFi?'))
            continue
        if meta.get(PIN_KEY):
            # Already on the door and applied again (new plan, returning member):
            # their newly chosen PIN replaces the old one.
            result.actions.append(Action('repin', m, str(uid), 'new PIN from online application'))
        status = user.get('status')
        if ok and status != 'ACTIVE':
            result.actions.append(Action('activate', m, str(uid), why))
        elif not ok and status == 'ACTIVE':
            result.actions.append(Action('deactivate', m, str(uid), why))

    # Circuit breaker: a Fabman outage or data glitch that suddenly makes many
    # members look unpaid must not lock everyone out. Hold ALL deactivations
    # this run and alert a human instead.
    deacts = result.of('deactivate')
    if len(deacts) > max_deactivations:
        result.held = True
        result.hold_reason = (
            f'{len(deacts)} members would lose door access in one run (limit {max_deactivations}); '
            'holding all deactivations until someone checks Fabman: '
            + ', '.join(name(a.member) for a in deacts)
        )
        result.actions = [a for a in result.actions if a.kind != 'deactivate']
    return result
