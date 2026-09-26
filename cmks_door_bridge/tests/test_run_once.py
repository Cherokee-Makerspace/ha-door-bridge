import base64
from datetime import date

from nacl.public import PrivateKey, SealedBox

from door_bridge.__main__ import DEFAULTS, run_once
from door_bridge.clients import ApiError


class FakeFabman:
    def __init__(self, members, unpaid=(), paid=()):
        self.m = {m['id']: m for m in members}
        self.unpaid, self.paid = list(unpaid), set(paid)

    def members(self):
        return list(self.m.values())

    def unpaid_invoices(self):
        return self.unpaid

    def has_paid_invoice(self, mid):
        return mid in self.paid

    def update_metadata(self, member, set_keys, drop_keys=()):
        meta = self.m[member['id']].setdefault('metadata', {})
        meta.update(set_keys)
        for k in drop_keys:
            meta.pop(k, None)


class FakeUnifi:
    def __init__(self, reject_pin=False):
        self.u, self.pins, self.reject_pin = {}, {}, reject_pin

    def users(self):
        return list(self.u.values())

    def create_user(self, first, last, email, emp):
        uid = f'u{len(self.u) + 1}'
        self.u[uid] = {'id': uid, 'status': 'ACTIVE', 'employee_number': str(emp)}
        return uid

    def set_pin(self, uid, pin):
        if self.reject_pin:
            class R:  # minimal response stand-in
                status_code, text, url = 400, 'dup', ''
                request = type('Q', (), {'method': 'PUT'})
            e = ApiError('UniFi', R)
            e.code = 'CODE_CREDS_PIN_CODE_CREDS_ALREADY_EXIST'
            raise e
        self.pins[uid] = pin

    def set_policies(self, uid, ids):
        self.u[uid]['policies'] = ids

    def set_status(self, uid, active):
        self.u[uid]['status'] = 'ACTIVE' if active else 'DEACTIVATED'


class Quiet:
    def __init__(self):
        self.said = []

    def say(self, t):
        self.said.append(t)


KEY = PrivateKey.generate()
SEALED = base64.b64encode(SealedBox(KEY.public_key).encrypt(b'48213')).decode()
CFG = {**DEFAULTS, 'dry_run': False, 'access_policy_ids': ['front-door']}
TODAY = date(2026, 9, 25)


def new_member(**meta):
    return {'id': 7, 'firstName': 'Ada', 'lastName': 'L', 'emailAddress': 'a@x', 'state': 'active',
            'metadata': {'cmks_pin_sealed': SEALED, **meta},
            '_embedded': {'memberPackages': [{'fromDate': '2026-09-01'}]}}


def test_paid_signup_gets_user_pin_policy_and_sealed_pin_is_dropped():
    fm, ua, n = FakeFabman([new_member()], paid={7}), FakeUnifi(), Quiet()
    run_once(CFG, fm, ua, KEY, n, TODAY)
    assert ua.pins == {'u1': '48213'}
    assert ua.u['u1']['policies'] == ['front-door'] and ua.u['u1']['status'] == 'ACTIVE'
    assert fm.m[7]['metadata'] == {'cmks_unifi_user_id': 'u1'}
    # second run: nothing new
    run_once(CFG, fm, ua, KEY, n, TODAY)
    assert len(ua.u) == 1


def test_then_unpaid_invoice_turns_them_off():
    fm, ua, n = FakeFabman([new_member()], paid={7}), FakeUnifi(), Quiet()
    run_once(CFG, fm, ua, KEY, n, TODAY)
    fm.unpaid = [{'member': 7, 'date': '2026-09-20', 'state': 'unpaid'}]  # older than the 3-day grace
    run_once(CFG, fm, ua, KEY, n, TODAY)
    assert ua.u['u1']['status'] == 'DEACTIVATED'
    assert any('OFF for Ada L' in s for s in n.said)


def test_rejected_pin_leaves_user_off_and_flags_staff_once():
    fm, ua, n = FakeFabman([new_member()], paid={7}), FakeUnifi(reject_pin=True), Quiet()
    run_once(CFG, fm, ua, KEY, n, TODAY)
    assert ua.u['u1']['status'] == 'DEACTIVATED'
    assert 'cmks_unifi_error' in fm.m[7]['metadata']
    assert any('rejected their PIN' in s for s in n.said)
    run_once(CFG, fm, ua, KEY, n, TODAY)  # must not switch the PIN-less user back on
    assert ua.u['u1']['status'] == 'DEACTIVATED'


def test_dry_run_changes_nothing():
    fm, ua, n = FakeFabman([new_member()], paid={7}), FakeUnifi(), Quiet()
    run_once({**CFG, 'dry_run': True}, fm, ua, KEY, n, TODAY)
    assert ua.u == {} and 'cmks_pin_sealed' in fm.m[7]['metadata']


def test_configured_private_key_overrides_file(tmp_path):
    from door_bridge.__main__ import load_key
    k = PrivateKey.generate()
    got = load_key(str(tmp_path), base64.b64encode(bytes(k)).decode())
    assert bytes(got) == bytes(k) and not (tmp_path / 'pin_private_key').exists()


def test_repin_replaces_pin_and_drops_sealed_copy():
    fm, ua, n = FakeFabman([new_member()], paid={7}), FakeUnifi(), Quiet()
    run_once(CFG, fm, ua, KEY, n, TODAY)                     # provisioned with 48213
    fm.m[7]['metadata']['cmks_pin_sealed'] = base64.b64encode(SealedBox(KEY.public_key).encrypt(b'551208')).decode()
    run_once(CFG, fm, ua, KEY, n, TODAY)
    assert ua.pins['u1'] == '551208' and 'cmks_pin_sealed' not in fm.m[7]['metadata']
    assert len(ua.u) == 1
