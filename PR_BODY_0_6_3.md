Title: diagnostics: include options, poll interval, scheduler mode cache, and header names

Summary
- Extend diagnostics export to aid troubleshooting while preserving privacy.

Details
- Redactions: existing token/cookie redaction retained.
- Added `entry_options`: exposes configured options (intervals, voltage, timeouts).
- Added coordinator info:
  - `update_interval_seconds`: current dynamic poll interval.
  - `last_scheduler_modes`: cached scheduler mode per charger serial (value only).
  - `headers_info.base_header_names`: names of default headers used by the client (values redacted).
  - `headers_info.has_scheduler_bearer`: whether a scheduler Bearer token can be derived (boolean only).

Security
- No header values or secrets are included; only header names and booleans.
- Tokens/cookies remain redacted in `entry_data`.

Version
- Bump to 0.6.3

