# Enphase Mobile/Web Shared Constants Endpoint

_Observed from the Enphase web app as a Firebase-hosted JSON document used for shared client configuration._

## Endpoint
```http
GET https://enlighten-mobile-38d22.firebaseio.com/enho_constants.json
```

## Authentication
- No session cookie or bearer token was required in the captured request.
- The captured request included `e-auth-token: null`, which suggests the endpoint is not tied to an authenticated user session.
- Response content type was JSON.

## Privacy and Redaction
- User-specific request metadata has been removed from this note, including IP address, browser user-agent, locale headers, and exact timestamp values.
- Internal identifiers that are not needed for understanding the endpoint have been redacted from examples.
- The observed payload did not contain site IDs, user IDs, names, addresses, charger serials, or other obvious personal data.

## Purpose
The payload appears to provide global constants for Enphase clients, including:
- localized support article links
- storefront URLs and merchandising labels
- SKU lists and feature toggles
- minimum supported app or firmware versions
- device naming strings for non-EV product families

This does not appear to be a site-scoped or charger-scoped API.

## Example Payload Excerpt
```json
{
  "AI_SAVINGS_DATA": {
    "AI_SAVINGS_METRICS_LOWER_LIMIT": 0.1,
    "AI_SAVINGS_METRICS_UPPER_LIMIT": 0.1
  },
  "CONNECTIVITY_DATA": {
    "ENV_SPECIAL_CHARACTERS": ["#", "$", "&", "%", "£", "+", "=", "\"", "\\", "€"],
    "MIN_ESW_FOR_ENCODING": "D8.3.5314"
  },
  "ENPHASE_STORE": {
    "US": {
      "en": "https://store.enphase.com/storefront/en-us"
    }
  },
  "ENSTORE_CONSTANTS": {
    "ENPHASE_CARE_MAINTAINER_ID": {
      "production": [
        {
          "company_id": "<redacted>"
        }
      ]
    },
    "ONE_MIN_TELEMETRY_SKU": "ONE-MIN-TELEMETRY"
  },
  "IQCP_DATA": {
    "APP_VERSION": "4.1.0",
    "COMMAND_RETRIES": 3,
    "FW_VERSION": "2.0.0"
  }
}
```

## Notable Sections
- `AI_SAVINGS_DATA`: thresholds and regional support content for AI savings/billing explanations.
- `CONNECTIVITY_DATA`: client validation constants and minimum supported software values.
- `ENPHASE_STORE`: country/language-specific store links.
- `ENSTORE_CONSTANTS`: store UI labels, SKU catalogs, media URLs, and feature flags.
- `IQCP_DATA`: balcony solar related version gates and product naming strings.

## Integration Notes
- This endpoint looks like static metadata and should be treated as cacheable if it is ever consumed.
- The payload did not expose EV charger runtime state, commands, schedules, or account-specific configuration.
- If reused by the integration later, only the specific constants needed by a feature should be extracted; the full document is broader than the EV charger domain.
