# Security & Privacy Notes

- Keep polling conservative (15s) to reduce server load.
- Never store or log raw cookies/e-auth-token; redact in diagnostics.
- User must supply headers; provide a reauth path for expiry.
- Only access required endpoints; do not call unrelated APIs.
