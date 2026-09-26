# door-bridge

Runs on the makerspace LAN (the Home Assistant box). Every minute it reads membership standing
from Fabman and makes UniFi Access match: provisions newly-paid online sign-ups with their PIN, and
turns door access off/on as members go unpaid/current. The full flow and the reasons behind each
rule are in `../membership-signup/membership-signup-handoff.md`.

```
python -m door_bridge          # loop
python -m door_bridge once     # single pass
python -m door_bridge pubkey   # print the PIN public key for MODX
```

## Install on Home Assistant (local add-on)

1. Settings → Apps → ⋮ → **Repositories** → add `https://github.com/Cherokee-Makerspace/ha-door-bridge`.
2. Install **Cherokee Makerspace Door Bridge** from the app store.
3. Configuration tab:
   - `fabman_api_key`: a new key just for the bridge (Fabman → Configure → Integrations).
   - `unifi_host`: the console's LAN IP. `unifi_token`: Access → Settings → General → Advanced →
     API Token, with scopes `view:user`, `edit:user`, `view:credential`, `view:policy`.
   - `access_policy_ids`: the member door policy's id (look it up at `GET /access_policies`).
   - `healthcheck_url`: a check at healthchecks.io (free), period 5 min, grace 5 min, with its
     email/Slack integration on. **This is what tells you the bridge or the HA box is down.**
   - `slack_webhook_url`: optional; it posts door ON/OFF changes and problems.
   - Leave `dry_run: true`.
4. **PIN key:** the key pair already exists. It was generated on Alex's Mac on 2026-09-25 so
   `/join` could open before the door hardware, and its public half is already in MODX
   (`cmks_pin_public_key`). Paste the private key into the add-on's `pin_private_key` option. It's kept
   in the makerspace's password manager. Start the add-on and check that the logged
   `PIN public key` matches `cmks_pin_public_key` in MODX. **If you leave the option blank, it
   generates a different key, and PINs from existing applications can't be decrypted.**
5. Watch the log for a day in dry run. Each run lists what it *would* change. When it looks
   right, set `dry_run: false`.

**Back up `/data/pin_private_key`** (it's in HA backups by default). If it's lost, approved but
not-yet-provisioned PINs can't be decrypted; generate a new key, update MODX, and ask those
members for a new PIN.

## Or: any Docker host

`.env` with `FABMAN_API_KEY=…`, `UNIFI_HOST=…`, `UNIFI_TOKEN=…`, `ACCESS_POLICY_IDS=id1,id2`,
`HEALTHCHECK_URL=…`, `DRY_RUN=true`, then `docker compose up -d`.

## Fabman metadata keys it uses

| key | set by | meaning |
|---|---|---|
| `cmks_pin_sealed` | MODX on approval | sealed PIN, waiting for first payment; deleted once on the door |
| `cmks_application_id` | MODX on approval | links back to the MODX application row |
| `cmks_unifi_user_id` | bridge | this member's UniFi Access user; only linked users are managed |
| `cmks_unifi_error` | bridge | UniFi rejected the PIN (usually a duplicate). Fix it in UniFi, then delete this key and `cmks_pin_sealed` |

To bring an **existing** door user under management, add `cmks_unifi_user_id` to their Fabman
metadata by hand.

## Tests

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest && .venv/bin/python -m pytest
```
