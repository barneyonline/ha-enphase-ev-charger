Title: tests: cover buttons, fast window, and latency/connectivity sensors

Summary
- Add unit tests to improve coverage of entity behavior and coordinator timing.

Details
- Buttons: verify Start/Stop button press calls underlying client with correct defaults and updates fast window.
- Fast window: ensure `kick_fast()` forces fast poll interval even when idle.
- Cloud latency: assert Cloud Latency sensor reflects `coord.latency_ms`.
- Site cloud reachable: assert state flips based on last successful update vs. interval.

Notes
- Tests avoid HA-heavy fixtures by stubbing coordinator and client where practical.

