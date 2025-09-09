Summary
- Fix TypeError when selecting Charge Mode by merging per-call headers in api._json() so aiohttp gets a single headers dict.
- Charge Mode selector now shows the Enphase scheduler preference (MANUAL/SCHEDULED/GREEN) via coordinator's charge_mode_pref.
- Bump manifest version to 0.2.5.

Details
- api.py: merge default headers with caller-provided ones (e.g., Authorization Bearer) and pass a single headers kwarg.
- coordinator.py: cache and expose scheduler preference as charge_mode_pref alongside charge_mode fallback.
- select.py: prefer charge_mode_pref for current_option display.

Validation
- Local tests passed (pytest tests_enphase_ev), ruff clean.
