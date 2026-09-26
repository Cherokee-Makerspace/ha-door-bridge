# Cherokee Makerspace: Home Assistant add-ons

Home Assistant add-on repository. Add it in **Settings → Apps → ⋮ → Repositories** with this
repository's URL.

| Add-on | What it does |
|---|---|
| [Door Bridge](cmks_door_bridge/) | Keeps UniFi Access door users in line with Fabman membership standing: sets new members' PINs once they've paid, and turns access off for unpaid members and back on when they're current. Runs on the makerspace LAN; nothing is exposed to the internet. |

This repo holds code only, no keys or member data. Every secret (Fabman API key, UniFi Access
token, PIN private key) is entered in the add-on's configuration on the Home Assistant box.

The source of truth is the makerspace's private website repo (`door-bridge/`), which this is
published from.
