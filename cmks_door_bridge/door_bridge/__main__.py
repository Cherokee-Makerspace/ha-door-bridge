"""Door bridge: keeps UniFi Access in line with Fabman membership standing.

    python -m door_bridge            run forever (every `interval_seconds`)
    python -m door_bridge once       one pass, then exit
    python -m door_bridge pubkey     print the public key for MODX's cmks_pin_public_key

Config comes from /data/options.json (Home Assistant add-on options) or, when
that file doesn't exist, from environment variables of the same names in
upper case (FABMAN_API_KEY, UNIFI_HOST, ...).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests
from nacl.public import PrivateKey, SealedBox

from . import reconcile
from .clients import ApiError, Fabman, UnifiAccess

log = logging.getLogger('door_bridge')

DEFAULTS = {
    'fabman_api_key': '',
    'fabman_api_url': 'https://fabman.io/api/v1',
    'fabman_space_id': 1710,
    'unifi_host': '',
    'unifi_token': '',
    'unifi_verify_tls': False,
    'access_policy_ids': [],
    'interval_seconds': 60,
    'grace_days': 3,
    'max_deactivations_per_run': 3,
    'dry_run': True,
    'healthcheck_url': '',
    'slack_webhook_url': '',
    'data_dir': '/data',
    # Optional: the PIN private key, base64. Overrides <data_dir>/pin_private_key.
    # The key pair was generated before the bridge existed (2026-09-25) so /join
    # could go live; paste that private key here when installing.
    'pin_private_key': '',
}


def load_config():
    cfg = dict(DEFAULTS)
    opts = Path('/data/options.json')
    if opts.exists():
        cfg.update(json.loads(opts.read_text()))
    else:
        for k, default in DEFAULTS.items():
            v = os.environ.get(k.upper())
            if v is None:
                continue
            if isinstance(default, bool):
                v = v.lower() in ('1', 'true', 'yes')
            elif isinstance(default, int):
                v = int(v)
            elif isinstance(default, list):
                v = [x.strip() for x in v.split(',') if x.strip()]
            cfg[k] = v
    return cfg


def load_key(data_dir, configured: str = '') -> PrivateKey:
    """The PIN private key: from config if given, else <data_dir>/pin_private_key,
    generated on first start."""
    if configured.strip():
        return PrivateKey(base64.b64decode(configured.strip()))
    path = Path(data_dir) / 'pin_private_key'
    if path.exists():
        return PrivateKey(base64.b64decode(path.read_text().strip()))
    key = PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(bytes(key)).decode())
    path.chmod(0o600)
    log.warning('Generated a new PIN key pair in %s', path)
    return key


def public_key_b64(key: PrivateKey) -> str:
    return base64.b64encode(bytes(key.public_key)).decode()


class Notifier:
    def __init__(self, cfg):
        self.slack = cfg['slack_webhook_url']
        self.hc = cfg['healthcheck_url'].rstrip('/')

    def say(self, text):
        log.info('NOTIFY %s', text)
        if self.slack:
            try:
                requests.post(self.slack, json={'text': f':door: {text}'}, timeout=10)
            except requests.RequestException as e:
                log.error('Slack notify failed: %s', e)

    def ping(self, ok=True, body=''):
        """Dead-man's switch (e.g. healthchecks.io): it alerts you when pings STOP,
        which catches the Home Assistant box being off, not just errors."""
        if not self.hc:
            return
        try:
            requests.post(self.hc + ('' if ok else '/fail'), data=body[:10000].encode(), timeout=10)
        except requests.RequestException as e:
            log.error('Healthcheck ping failed: %s', e)


def run_once(cfg, fabman: Fabman, unifi: UnifiAccess, key: PrivateKey, notify: Notifier, today=None):
    today = today or date.today()
    members = fabman.members()
    unpaid = fabman.unpaid_invoices()
    users = unifi.users()

    pending = [m for m in members
               if (m.get('metadata') or {}).get(reconcile.PIN_KEY)
               and not (m.get('metadata') or {}).get(reconcile.UNIFI_KEY)]
    paid = {int(m['id']) for m in pending if fabman.has_paid_invoice(m['id'])}

    p = reconcile.plan(members, users, unpaid, paid, today,
                       grace_days=cfg['grace_days'], max_deactivations=cfg['max_deactivations_per_run'])
    dry = cfg['dry_run']
    tag = '[dry run] ' if dry else ''
    if p.held:
        notify.say(f'{tag}HOLD: {p.hold_reason}')

    for a in p.actions:
        who = reconcile.name(a.member)
        log.info('%s%s %s (%s)', tag, a.kind, who, a.reason)
        if dry:
            continue
        try:
            if a.kind == 'provision':
                provision(a.member, fabman, unifi, key, cfg, notify)
            elif a.kind == 'repin':
                repin(a.member, a.unifi_user_id, fabman, unifi, key, notify)
            elif a.kind == 'link':
                fabman.update_metadata(a.member, {reconcile.UNIFI_KEY: a.unifi_user_id})
                notify.say(f'Linked {who} to existing UniFi user {a.unifi_user_id} ({a.reason}); their new PIN is set on the next run.')
            elif a.kind in ('activate', 'deactivate'):
                unifi.set_status(a.unifi_user_id, a.kind == 'activate')
                notify.say(f"Door access {'ON' if a.kind == 'activate' else 'OFF'} for {who} — {a.reason}.")
        except ApiError as e:
            notify.say(f'{a.kind} failed for {who}: {e}')

    summary = (f'{tag}{len(members)} Fabman members, {len(users)} UniFi users, '
               f"{len(p.actions)} changes, {len(p.waiting)} waiting" + (', HELD' if p.held else ''))
    for m, why in p.waiting:
        log.info('waiting: %s — %s', reconcile.name(m), why)
    return summary


def provision(member, fabman, unifi, key, cfg, notify):
    who = reconcile.name(member)
    sealed = member['metadata'][reconcile.PIN_KEY]
    pin = SealedBox(key).decrypt(base64.b64decode(sealed)).decode()
    uid = unifi.create_user(member.get('firstName') or '', member.get('lastName') or '',
                            member.get('emailAddress') or '', member['id'])
    # Written back immediately: if anything below fails, the next run sees the
    # link and won't create a second UniFi user.
    fabman.update_metadata(member, {reconcile.UNIFI_KEY: uid})
    try:
        unifi.set_pin(uid, pin)
    except ApiError as e:
        unifi.set_status(uid, False)
        fabman.update_metadata(member, {reconcile.ERROR_KEY: f'PIN rejected by UniFi ({getattr(e, "code", e.status)})'})
        notify.say(f'{who} is paid up but UniFi rejected their PIN ({getattr(e, "code", e.status)} — '
                   'probably already used by someone else). Set a PIN for them in UniFi Access, '
                   f'then delete {reconcile.ERROR_KEY} and {reconcile.PIN_KEY} from their Fabman metadata.')
        return
    finally:
        del pin
    if cfg['access_policy_ids']:
        unifi.set_policies(uid, cfg['access_policy_ids'])
    unifi.set_status(uid, True)
    fabman.update_metadata(member, {}, drop_keys=(reconcile.PIN_KEY,))
    notify.say(f'New member {who} is on the door (UniFi user {uid}).')


def repin(member, uid, fabman, unifi, key, notify):
    who = reconcile.name(member)
    pin = SealedBox(key).decrypt(base64.b64decode(member['metadata'][reconcile.PIN_KEY])).decode()
    try:
        unifi.set_pin(uid, pin)
    except ApiError as e:
        fabman.update_metadata(member, {reconcile.ERROR_KEY: f'new PIN rejected by UniFi ({getattr(e, "code", e.status)})'})
        notify.say(f'UniFi rejected the new PIN for {who} ({getattr(e, "code", e.status)}); their old PIN still works. '
                   f'Set one in UniFi Access, then delete {reconcile.ERROR_KEY} and {reconcile.PIN_KEY} from their Fabman metadata.')
        return
    finally:
        del pin
    fabman.update_metadata(member, {}, drop_keys=(reconcile.PIN_KEY,))
    notify.say(f'Updated the door PIN for {who}.')


def main(argv):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config()
    key = load_key(cfg['data_dir'], cfg['pin_private_key'])
    cmd = argv[1] if len(argv) > 1 else 'run'
    if cmd == 'pubkey':
        print(public_key_b64(key))
        return 0
    log.info('PIN public key (paste into MODX System Setting cmks_pin_public_key): %s', public_key_b64(key))

    missing = [k for k in ('fabman_api_key', 'unifi_host', 'unifi_token') if not cfg[k]]
    if missing:
        log.error('Missing config: %s', ', '.join(missing))
        return 2
    if cfg['dry_run']:
        log.warning('DRY RUN: logging what would change, touching nothing. Set dry_run: false when the log looks right.')

    fabman = Fabman(cfg['fabman_api_key'], cfg['fabman_api_url'], cfg['fabman_space_id'])
    unifi = UnifiAccess(cfg['unifi_host'], cfg['unifi_token'], cfg['unifi_verify_tls'])
    notify = Notifier(cfg)
    failures = 0
    while True:
        try:
            summary = run_once(cfg, fabman, unifi, key, notify)
            log.info(summary)
            notify.ping(True, summary)
            failures = 0
        except Exception as e:  # keep looping; the healthcheck and Slack say it's broken
            failures += 1
            log.exception('run failed')
            notify.ping(False, repr(e))
            if failures in (3, 30):  # a few minutes, then ~half an hour
                notify.say(f'Door sync has failed {failures} times in a row: {e!r}. '
                           'No access changes are being made until it recovers.')
        if cmd == 'once':
            return 0
        time.sleep(cfg['interval_seconds'])


if __name__ == '__main__':
    sys.exit(main(sys.argv))
