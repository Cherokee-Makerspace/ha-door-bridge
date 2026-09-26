from datetime import date

from door_bridge import reconcile as r

TODAY = date(2026, 9, 25)


def member(mid, state='active', packages=(('2026-01-01', None),), **meta):
    return {
        'id': mid, 'firstName': 'M', 'lastName': str(mid), 'state': state,
        'metadata': meta,
        '_embedded': {'memberPackages': [{'fromDate': f, 'untilDate': u} for f, u in packages]},
    }


def user(uid, status='ACTIVE', emp=None):
    return {'id': uid, 'status': status, 'employee_number': emp}


def inv(mid, day, state='unpaid'):
    return {'member': mid, 'date': day, 'state': state}


def kinds(p):
    return sorted((a.kind, a.member['id']) for a in p.actions)


def test_unlinked_members_and_users_are_never_touched():
    p = r.plan([member(1)], [user('u9', 'DEACTIVATED')], [inv(1, '2026-09-01')], set(), TODAY)
    assert p.actions == [] and p.waiting == []


def test_unpaid_invoice_turns_access_off():
    p = r.plan([member(1, cmks_unifi_user_id='u1')], [user('u1')], [inv(1, '2026-09-20')], set(), TODAY)
    assert kinds(p) == [('deactivate', 1)]
    assert p.actions[0].reason == 'unpaid invoice'


def test_paying_turns_access_back_on():
    p = r.plan([member(1, cmks_unifi_user_id='u1')], [user('u1', 'DEACTIVATED')], [], set(), TODAY)
    assert kinds(p) == [('activate', 1)]


def test_grace_days_delay_lockout():
    m, u, i = [member(1, cmks_unifi_user_id='u1')], [user('u1')], [inv(1, '2026-09-23')]
    assert kinds(r.plan(m, u, i, set(), TODAY, grace_days=3)) == []
    assert kinds(r.plan(m, u, i, set(), TODAY, grace_days=2)) == [('deactivate', 1)]


def test_pending_or_processing_payment_is_not_unpaid():
    for state in ('pending', 'processing', 'paid'):
        p = r.plan([member(1, cmks_unifi_user_id='u1')], [user('u1')], [inv(1, '2026-09-01', state)], set(), TODAY)
        assert p.actions == []


def test_locked_member_or_expired_package_loses_access():
    p = r.plan(
        [member(1, state='locked', cmks_unifi_user_id='u1'),
         member(2, packages=(('2025-01-01', '2026-09-24'),), cmks_unifi_user_id='u2'),
         member(3, packages=(), cmks_unifi_user_id='u3')],
        [user('u1'), user('u2'), user('u3')], [], set(), TODAY)
    assert kinds(p) == [('deactivate', 1), ('deactivate', 2), ('deactivate', 3)]


def test_future_package_is_not_active_yet():
    p = r.plan([member(1, packages=(('2026-10-01', None),), cmks_unifi_user_id='u1')], [user('u1')], [], set(), TODAY)
    assert kinds(p) == [('deactivate', 1)]


def test_new_signup_waits_for_first_payment():
    m = member(1, cmks_pin_sealed='x')
    p = r.plan([m], [], [], set(), TODAY)
    assert p.actions == [] and p.waiting[0][1] == 'waiting for first payment'
    p = r.plan([m], [], [], {1}, TODAY)
    assert kinds(p) == [('provision', 1)]


def test_new_signup_with_unpaid_invoice_waits_even_if_older_invoice_paid():
    p = r.plan([member(1, cmks_pin_sealed='x')], [], [inv(1, '2026-09-20')], {1}, TODAY)
    assert p.actions == [] and p.waiting[0][1] == 'unpaid invoice'


def test_half_finished_provision_links_instead_of_duplicating():
    p = r.plan([member(1, cmks_pin_sealed='x')], [user('u7', emp='1')], [], {1}, TODAY)
    assert kinds(p) == [('link', 1)] and p.actions[0].unifi_user_id == 'u7'


def test_pin_error_waits_for_staff():
    p = r.plan([member(1, cmks_pin_sealed='x', cmks_unifi_error='dup')], [], [], {1}, TODAY)
    assert p.actions == [] and 'needs staff' in p.waiting[0][1]


def test_linked_user_missing_in_unifi_is_reported_not_recreated():
    p = r.plan([member(1, cmks_unifi_user_id='gone')], [], [], set(), TODAY)
    assert p.actions == [] and 'not found' in p.waiting[0][1]


def test_circuit_breaker_holds_mass_lockout_but_not_other_changes():
    members = [member(i, cmks_unifi_user_id=f'u{i}') for i in range(1, 6)] + [member(9, cmks_unifi_user_id='u9')]
    users = [user(f'u{i}') for i in range(1, 6)] + [user('u9', 'DEACTIVATED')]
    unpaid = [inv(i, '2026-09-01') for i in range(1, 6)]
    p = r.plan(members, users, unpaid, set(), TODAY, max_deactivations=3)
    assert p.held and '5 members' in p.hold_reason
    assert kinds(p) == [('activate', 9)]
    assert not r.plan(members, users, unpaid, set(), TODAY, max_deactivations=5).held


def test_dependent_loses_access_when_payer_is_unpaid():
    dep = member(2, cmks_unifi_user_id='u2')
    dep['paidForBy'] = 1
    p = r.plan([member(1), dep], [user('u2')], [inv(1, '2026-09-01')], set(), TODAY)
    assert kinds(p) == [('deactivate', 2)] and 'pays for them' in p.actions[0].reason


def test_default_grace_is_three_days():
    m, u = [member(1, cmks_unifi_user_id='u1')], [user('u1')]
    assert kinds(r.plan(m, u, [inv(1, '2026-09-23')], set(), TODAY)) == []
    assert kinds(r.plan(m, u, [inv(1, '2026-09-22')], set(), TODAY)) == [('deactivate', 1)]


def test_new_pin_for_someone_already_on_the_door():
    p = r.plan([member(1, cmks_unifi_user_id='u1', cmks_pin_sealed='x')], [user('u1')], [], set(), TODAY)
    assert kinds(p) == [('repin', 1)]


def test_existing_unifi_user_found_by_email_is_linked_not_duplicated():
    m = member(1, cmks_pin_sealed='x'); m['emailAddress'] = ' Pat@Example.com'
    u = {'id': 'u5', 'status': 'ACTIVE', 'user_email': 'pat@example.com'}
    p = r.plan([m], [u], [], {1}, TODAY)
    assert kinds(p) == [('link', 1)] and p.actions[0].unifi_user_id == 'u5'
    # next run: linked + sealed PIN -> repin
    m['metadata']['cmks_unifi_user_id'] = 'u5'
    assert kinds(r.plan([m], [u], [], {1}, TODAY)) == [('repin', 1)]


def test_two_unifi_users_with_same_email_wait_for_staff():
    m = member(1, cmks_pin_sealed='x'); m['emailAddress'] = 'pat@example.com'
    us = [{'id': 'a', 'user_email': 'pat@example.com'}, {'id': 'b', 'user_email': 'PAT@example.com'}]
    p = r.plan([m], us, [], {1}, TODAY)
    assert p.actions == [] and 'share this email' in p.waiting[0][1]


def test_invoice_without_a_member_is_ignored():
    # Seen live 2026-09-26: Fabman has unpaid invoices with member = null (walk-in/guest).
    p = r.plan([member(1, cmks_unifi_user_id='u1')], [user('u1')],
               [{'member': None, 'date': '2026-09-01', 'state': 'unpaid'}, inv(1, '2026-09-01')], set(), TODAY)
    assert kinds(p) == [('deactivate', 1)]
