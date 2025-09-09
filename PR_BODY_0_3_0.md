Summary
- Charging Level sensor updated to "Charging Amps" with proper units and device class; unknown values now show 0.
- Add a temporary fast-poll window after Start/Stop actions to reflect state changes quickly.
- Remove unreliable sensors that rarely return data across deployments.

Details
- sensor.py:
  - Charging Level -> Charging Amps: device_class=current, unit=A; value coerced to int; fallback to last_set_amps; unknown=0.
  - Removed entities: Connector Reason, Schedule Type, Schedule Start, Schedule End, Session Miles, Session Plug-in At, Session Plug-out At.
- coordinator.py/button.py/__init__.py:
  - New `kick_fast()` on coordinator; called after start/stop to poll faster for a short window.

Breaking changes
- The following sensors were removed due to consistently missing or unreliable values in most regions:
  - sensor.<device>_connector_reason
  - sensor.<device>_schedule_type
  - sensor.<device>_schedule_start
  - sensor.<device>_schedule_end
  - sensor.<device>_session_miles
  - sensor.<device>_session_plug_in_at
  - sensor.<device>_session_plug_out_at
- Charging Level sensor was renamed and its unique_id changed to represent amps; dashboards/automations referencing the old entity may need to be updated to the new "Charging Amps" sensor.

Context
- v0.2.6 handled benign 4xx on Start/Stop and aligned Charge Mode sensor/select with the scheduler preference.
- v0.3.0 builds on that with better responsiveness during state transitions and clearer/current-focused amperage reporting.

Validation
- Lint clean with ruff. Local tests updated in prior PRs.
