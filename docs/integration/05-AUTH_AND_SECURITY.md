# Auth & Security

- Store `e_auth_token` and `cookie` using HA’s credential store (via Config Entries; mark fields as `SECRET`).  
- **Never** log header values. Redact diagnostics.
- Refresh strategy:
  - When `/status` returns 401, mark entry as needing reauth (Repair flow prompts user to paste fresh headers).
- Note that these are **session-bound** tokens. The integration does **not** perform a login.
- Respect user privacy; only call the listed endpoints, at the configured interval.
