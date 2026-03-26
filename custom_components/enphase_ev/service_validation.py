from __future__ import annotations

from homeassistant.exceptions import ServiceValidationError


def raise_translated_service_validation(
    *,
    translation_domain: str,
    translation_key: str,
    translation_placeholders: dict[str, object] | None = None,
    message: str | None = None,
) -> None:
    """Raise a translated service validation error."""

    kwargs: dict[str, object] = {
        "translation_domain": translation_domain,
        "translation_key": translation_key,
        "translation_placeholders": translation_placeholders,
    }
    if message is None:
        raise ServiceValidationError(**kwargs)
    raise ServiceValidationError(message, **kwargs)
