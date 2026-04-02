# Security Policy

## Supported Versions

Security fixes are only guaranteed for the latest published release and the current `main` branch.

| Version | Supported |
| ------- | --------- |
| Latest release | Yes |
| `main` | Yes |
| Older releases | No |

If you are running an older release, update to the latest published version before reporting a security issue unless the issue prevents you from upgrading safely.

## Reporting a Vulnerability

Do not open public GitHub issues, discussions, or pull requests for suspected security vulnerabilities.

Use GitHub's private vulnerability reporting for this repository:

1. Open the repository's `Security` tab.
2. Choose `Report a vulnerability`.
3. Include a clear description of the issue, affected versions, impact, and reproduction steps.
4. Attach sanitized logs, diagnostics, screenshots, or sample payloads if they help explain the issue.

Please include:

- The integration version and Home Assistant version.
- The affected device types or Enphase features.
- Any required configuration or environment details.
- Whether the issue exposes credentials, tokens, personal data, or device control.
- Any mitigation you have already confirmed.

Response targets:

- Initial triage response within 7 days.
- Status updates at least every 14 days while the report is being investigated.

If the report is accepted, the maintainer will work on a fix, prepare a release, and coordinate disclosure timing when needed. If the report is declined, you will receive a short explanation so you can decide whether to provide more detail or pursue a non-security bug report instead.
