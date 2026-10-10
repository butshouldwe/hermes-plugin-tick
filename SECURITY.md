# Security policy

## Supported versions

Pre-1.0. Only the latest revision of `main` receives fixes; there is no
back-porting.

## Reporting a vulnerability

Report privately through GitHub Security Advisories: use "Report a
vulnerability" under the Security tab at
[github.com/butshouldwe/hermes-plugin-tick/security/advisories/new](https://github.com/butshouldwe/hermes-plugin-tick/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Expect an acknowledgment within 7 days.

## Scope

In scope:

- The agent half (`__init__.py`): anything that lets injected context leak
  data across profiles or sessions, or alter the system prompt or history.
- The dashboard backend (`dashboard/plugin_api.py`): input validation and
  writes to plugin state.
- The desktop companion (`desktop/plugin.js`): it must send only the IANA zone
  name, and only when the user enabled it.

Out of scope: Hermes itself and the desktop app. Report those upstream at
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).
