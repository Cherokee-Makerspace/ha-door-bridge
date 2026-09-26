"""Thin HTTP clients for Fabman (cloud) and UniFi Access (LAN)."""
from __future__ import annotations

import time

import requests
import urllib3


class ApiError(RuntimeError):
    def __init__(self, service, resp):
        self.status = resp.status_code
        self.body = resp.text[:500]
        super().__init__(f'{service} {resp.request.method} {resp.url} -> {resp.status_code}: {self.body}')


class Fabman:
    """https://fabman.io/api/v1/documentation"""

    def __init__(self, api_key, base_url='https://fabman.io/api/v1', space_id=None, timeout=20):
        self.base = base_url.rstrip('/') + '/'
        self.space_id = space_id
        self.s = requests.Session()
        self.s.headers.update({'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'})
        self.timeout = timeout

    def _req(self, method, path_or_url, **kw):
        url = path_or_url if path_or_url.startswith('http') else self.base + path_or_url
        for attempt in range(3):
            resp = self.s.request(method, url, timeout=self.timeout, **kw)
            if resp.status_code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))  # docs: wait at least 2s
                continue
            if resp.status_code >= 400:
                raise ApiError('Fabman', resp)
            return resp

    def _all(self, path, params):
        """Follow RFC 5988 Link: rel=next pagination."""
        out, url, params = [], path, {**params, 'limit': 100}
        while url:
            resp = self._req('GET', url, params=params)
            out.extend(resp.json())
            url = resp.links.get('next', {}).get('url')
            params = None
        return out

    def members(self):
        # GET /members rejects a `space` filter (400 "space is not allowed", seen
        # live 2026-09-26); the account has one space, so list everything.
        return self._all('members', {'embed': 'activePackages'})

    def unpaid_invoices(self):
        return self._all('invoices', {'state': 'unpaid'})

    def has_paid_invoice(self, member_id) -> bool:
        return bool(self._req('GET', 'invoices', params={'member': member_id, 'state': 'paid', 'limit': 1}).json())

    def update_metadata(self, member: dict, set_keys: dict, drop_keys=()):
        """PUT metadata with the member's lockVersion (Fabman's optimistic lock)."""
        current = self._req('GET', f"members/{member['id']}").json()
        meta = dict(current.get('metadata') or {})
        meta.update(set_keys)
        for k in drop_keys:
            meta.pop(k, None)
        self._req('PUT', f"members/{member['id']}", json={'lockVersion': current['lockVersion'], 'metadata': meta})


class UnifiAccess:
    """UniFi Access developer API (LAN only), https://<console>:12445/api/v1/developer."""

    def __init__(self, host, token, verify_tls=False, port=12445, timeout=15):
        self.base = f'https://{host}:{port}/api/v1/developer/'
        self.s = requests.Session()
        self.s.headers.update({'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
        self.s.verify = verify_tls
        if not verify_tls:
            # The console ships a self-signed cert. Traffic stays on the LAN.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.timeout = timeout

    def _req(self, method, path, **kw):
        resp = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or body.get('code') not in (None, 'SUCCESS'):
            err = ApiError('UniFi', resp)
            err.code = body.get('code')
            raise err
        return body.get('data')

    def users(self):
        out, page = [], 1
        while True:
            batch = self._req('GET', 'users', params={'page_num': page, 'page_size': 100}) or []
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def create_user(self, first, last, email, employee_number):
        data = self._req('POST', 'users', json={
            'first_name': first, 'last_name': last,
            'user_email': email, 'employee_number': str(employee_number),
        })
        return str(data['id'])

    def set_pin(self, user_id, pin):
        self._req('PUT', f'users/{user_id}/pin_codes', json={'pin_code': pin})

    def set_policies(self, user_id, policy_ids):
        self._req('PUT', f'users/{user_id}/access_policies', json={'access_policy_ids': list(policy_ids)})

    def set_status(self, user_id, active: bool):
        self._req('PUT', f'users/{user_id}', json={'status': 'ACTIVE' if active else 'DEACTIVATED'})
