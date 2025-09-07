
from __future__ import annotations

async def async_get_config_entry_diagnostics(hass, entry):
    data = dict(entry.data)
    for k in ("e_auth_token", "cookie"):
        if k in data:
            data[k] = "***redacted***"
    return {"entry_data": data}
