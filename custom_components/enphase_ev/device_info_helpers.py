from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def _normalize_evse_model_name(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None

    text_upper = text.upper()
    if not text_upper.startswith("IQ-EVSE-"):
        return text

    parts = text_upper.split("-")
    if (
        len(parts) >= 5
        and parts[0] == "IQ"
        and parts[1] == "EVSE"
        and parts[2]
        and parts[3].isdigit()
        and len(parts[3]) == 4
    ):
        return "-".join(parts[:4])

    return text


def _compose_charger_model_display(
    display_name: str | None,
    model_name: object,
    fallback_name: str | None = None,
) -> str | None:
    display = display_name.strip() if isinstance(display_name, str) else None
    if not display:
        display = None
    fallback = fallback_name.strip() if isinstance(fallback_name, str) else None
    if not fallback:
        fallback = None
    model = _normalize_evse_model_name(model_name)
    if display and model:
        if model.casefold() in display.casefold():
            return display
        return f"{display} ({model})"
    if model:
        return model
    if display:
        return display
    return fallback


def _cloud_device_info(site_id: object) -> DeviceInfo:
    """Return DeviceInfo for cloud-level connectivity entities."""
    try:
        site_text = str(site_id).strip()
    except Exception:
        site_text = ""
    if not site_text:
        site_text = "unknown"
    return DeviceInfo(
        identifiers={(DOMAIN, f"type:{site_text}:cloud")},
        manufacturer="Enphase",
        name="Enphase Cloud",
        model="Cloud Service",
    )
