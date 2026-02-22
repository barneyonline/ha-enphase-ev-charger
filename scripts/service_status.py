#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import http.cookiejar
import socket
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://enlighten.enphaseenergy.com"
ENTREZ_URL = "https://entrez.enphaseenergy.com"
LOGIN_URL = f"{BASE_URL}/login/login.json"
SITE_SEARCH_URL = f"{BASE_URL}/app-api/search_sites.json?searchText=&favourite=false"

DEFAULT_TIMEOUT = 10


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    method: str
    url: str
    group: str
    headers: dict[str, str] | None = None
    form: dict[str, Any] | None = None
    json_body: Any | None = None
    affects_status: bool = True
    check_group: str | None = None
    check_mode: str = "all"  # "all" or "any"
    ok_statuses: tuple[int, ...] = (200,)


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_width(text: str) -> int:
    return max(30, 6 * len(text) + 10)


def _badge_svg(label: str, value: str, color: str) -> str:
    label_w = _text_width(label)
    value_w = _text_width(value)
    total_w = label_w + value_w
    label_x = label_w // 2
    value_x = label_w + (value_w // 2)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" '
        f'role="img" aria-label="{label}: {value}">'
        f'<linearGradient id="s" x2="0" y2="100%">'
        f'<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        f'<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<rect width="{label_w}" height="20" fill="#555"/>'
        f'<rect x="{label_w}" width="{value_w}" height="20" fill="{color}"/>'
        f'<rect width="{total_w}" height="20" fill="url(#s)"/>'
        f'<g fill="#fff" text-anchor="middle" '
        f'font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{label_x}" y="14">{label}</text>'
        f'<text x="{value_x}" y="14">{value}</text>'
        f"</g></svg>"
    )


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _jwt_user_id(token: str | None) -> str | None:
    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("user_id", "userId", "userid"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("user_id", "userId", "userid"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _jwt_session_id(token: str | None) -> str | None:
    if not token:
        return None
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    for key in ("session_id", "sessionId", "session"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("session_id", "sessionId", "session"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return None


def _cookie_header_for(url: str, jar: http.cookiejar.CookieJar) -> str:
    req = urllib.request.Request(url)
    try:
        jar.add_cookie_header(req)
    except Exception:
        return ""
    return req.get_header("Cookie") or ""


def _cookie_map(cookie_header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key:
            out[key] = value
    return out


def _extract_xsrf_token(cookies: dict[str, str]) -> str | None:
    for key, value in cookies.items():
        if key.lower() == "xsrf-token":
            return value
    return None


def _extract_bearer(cookies: dict[str, str]) -> str | None:
    return cookies.get("enlighten_manager_token_production")


def _request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    json_body: Any | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int | None, dict[str, str], bytes, str | None]:
    req_headers = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        req_headers.setdefault(
            "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
        )

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read()
            return status, dict(resp.headers), body, None
    except urllib.error.HTTPError as err:
        try:
            body = err.read()
        except Exception:
            body = b""
        return err.code, dict(err.headers), body, f"HTTP {err.code}"
    except (urllib.error.URLError, socket.timeout) as err:
        return None, {}, b"", str(err)


def _parse_json(body: bytes) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _with_query(url: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return url
    encoded = urllib.parse.urlencode(params)
    if not encoded:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{encoded}"


def _normalize_sites(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        for key in ("sites", "data", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
    items = payload if isinstance(payload, list) else []
    sites: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        site_id = (
            item.get("site_id")
            or item.get("siteId")
            or item.get("site")
            or item.get("id")
        )
        if site_id:
            sites.append(str(site_id))
    return sites


def _normalize_chargers(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        payload = (
            payload.get("data")
            or payload.get("chargers")
            or payload.get("evChargerData")
            or payload
        )
    items = payload if isinstance(payload, list) else []
    chargers: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        serial = (
            item.get("serial")
            or item.get("serialNumber")
            or item.get("sn")
            or item.get("id")
        )
        if serial:
            chargers.append(str(serial))
    return chargers


def _endpoint_result(
    spec: EndpointSpec,
    status: int | None,
    error: str | None,
    *,
    site_id: str | None = None,
    serial: str | None = None,
) -> dict[str, Any]:
    def _safe_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path or ""
        query = parsed.query or ""
        if path and (site_id or serial):
            parts = path.split("/")
            for idx, part in enumerate(parts):
                if site_id and part == site_id:
                    parts[idx] = "{site_id}"
                if serial and part == serial:
                    parts[idx] = "{serial}"
            path = "/".join(parts)
        if query:
            if site_id:
                query = query.replace(site_id, "{site_id}")
            if serial:
                query = query.replace(serial, "{serial}")
        if query:
            return f"{path}?{query}"
        return path

    ok_statuses = spec.ok_statuses or (200,)
    ok = status in ok_statuses if status is not None else False
    if ok:
        error = None

    return {
        "name": spec.name,
        "method": spec.method,
        "url": _safe_url(spec.url),
        "group": spec.group,
        "status": status,
        "ok": ok,
        "error": error,
        "check_group": spec.check_group,
        "check_mode": spec.check_mode,
        "affects_status": spec.affects_status,
    }


def _evaluate_status(results: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in results:
        key = item.get("check_group") or item["name"]
        entry = groups.get(key)
        if not entry:
            entry = {
                "name": key,
                "mode": item.get("check_mode") or "all",
                "group": item.get("group"),
                "affects": bool(item.get("affects_status", True)),
                "endpoints": [],
            }
            groups[key] = entry
        entry["endpoints"].append(item)

    checks: list[dict[str, Any]] = []
    for key, entry in groups.items():
        oks = [bool(ep.get("ok")) for ep in entry["endpoints"]]
        if entry["mode"] == "any":
            ok = any(oks)
        else:
            ok = all(oks) if oks else False
        checks.append(
            {
                "name": key,
                "group": entry["group"],
                "ok": ok,
                "affects": entry["affects"],
                "endpoints": [ep["name"] for ep in entry["endpoints"]],
            }
        )

    affecting = [c for c in checks if c["affects"]]
    all_down = bool(affecting) and all(not c["ok"] for c in affecting)
    main_down = any(c["group"] == "main" and not c["ok"] for c in affecting)
    degraded_down = any(
        c["group"] in ("degraded", "other") and not c["ok"] for c in affecting
    )

    if main_down or all_down:
        status = "Down"
    elif degraded_down:
        status = "Degraded"
    else:
        status = "Fully Operational"

    summary = {
        "checks_total": len(checks),
        "checks_ok": sum(1 for c in checks if c["ok"]),
        "checks_failed": sum(1 for c in checks if not c["ok"]),
        "endpoints_total": len(results),
        "endpoints_ok": sum(1 for r in results if r["ok"]),
        "endpoints_failed": sum(1 for r in results if not r["ok"]),
    }
    return status, {"checks": checks, "summary": summary}


def _write_outputs(
    output_dir: str,
    *,
    status: str,
    payload: dict[str, Any],
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    status_path = os.path.join(output_dir, "status.json")
    svg_path = os.path.join(output_dir, "status.svg")

    with open(status_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")

    color_map = {
        "Fully Operational": "#4c1",
        "Degraded": "#dfb317",
        "Down": "#e05d44",
    }
    label = "Enphase Service Status"
    svg = _badge_svg(label, status, color_map.get(status, "#9f9f9f"))
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write(svg)
        fh.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Enphase Energy service endpoints."
    )
    parser.add_argument("--output-dir", default="status-out")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--site-id")
    parser.add_argument("--serial")
    parser.add_argument("--locale")
    args = parser.parse_args()

    email = (os.environ.get("ENPHASE_EMAIL") or "").strip()
    password = (os.environ.get("ENPHASE_PASSWORD") or "").strip()
    site_id = (args.site_id or os.environ.get("ENPHASE_SITE_ID") or "").strip()
    serial = (args.serial or os.environ.get("ENPHASE_SERIAL") or "").strip()
    locale = (args.locale or os.environ.get("ENPHASE_LOCALE") or "en-US").strip()

    results: list[dict[str, Any]] = []
    started_at = _iso_utc_now()

    if not email or not password:
        status = "Down"
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Missing ENPHASE_EMAIL or ENPHASE_PASSWORD",
            "summary": {},
            "checks": [],
            "endpoints": [],
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Login
    login_spec = EndpointSpec(
        name="auth_login",
        method="POST",
        url=LOGIN_URL,
        group="other",
        form={"user[email]": email, "user[password]": password},
    )
    status_code, _headers, body, error = _request(
        opener,
        login_spec.method,
        login_spec.url,
        headers={"Accept": "application/json, text/plain, */*"},
        form=login_spec.form,
        timeout=args.timeout,
    )
    results.append(
        _endpoint_result(login_spec, status_code, error, site_id=site_id, serial=serial)
    )
    payload_json = _parse_json(body) or {}

    if status_code != 200:
        status = "Down"
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Login failed",
            "summary": {},
            "checks": [],
            "endpoints": results,
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    if isinstance(payload_json, dict) and payload_json.get("requires_mfa"):
        status = "Down"
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Account requires MFA; workflow cannot continue",
            "summary": {},
            "checks": [],
            "endpoints": results,
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    if isinstance(payload_json, dict) and payload_json.get("isBlocked") is True:
        status = "Down"
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Account is blocked",
            "summary": {},
            "checks": [],
            "endpoints": results,
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    if isinstance(payload_json, dict) and payload_json.get("success") is False:
        status = "Down"
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Login rejected",
            "summary": {},
            "checks": [],
            "endpoints": results,
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    session_id = None
    if isinstance(payload_json, dict):
        session_id = (
            payload_json.get("session_id")
            or payload_json.get("sessionId")
            or payload_json.get("session")
        )

    cookie_header = "; ".join(
        h for h in (_cookie_header_for(BASE_URL, jar), _cookie_header_for(ENTREZ_URL, jar)) if h
    )
    cookie_map = _cookie_map(cookie_header)
    xsrf = _extract_xsrf_token(cookie_map)

    # Token
    eauth = None
    if session_id:
        token_spec = EndpointSpec(
            name="auth_token",
            method="POST",
            url=f"{ENTREZ_URL}/tokens",
            group="other",
            json_body={"session_id": session_id, "email": email},
            ok_statuses=(200, 400, 404, 422, 429),
        )
        status_code, _headers, body, error = _request(
            opener,
            token_spec.method,
            token_spec.url,
            headers={"Accept": "application/json"},
            json_body=token_spec.json_body,
            timeout=args.timeout,
        )
        results.append(
            _endpoint_result(
                token_spec, status_code, error, site_id=site_id, serial=serial
            )
        )
        token_payload = _parse_json(body)
        if isinstance(token_payload, dict):
            token = (
                token_payload.get("token")
                or token_payload.get("auth_token")
                or token_payload.get("access_token")
            )
            if token:
                eauth = str(token)
    else:
        # Record a synthetic failure for visibility when session id is missing.
        token_spec = EndpointSpec(
            name="auth_token",
            method="POST",
            url=f"{ENTREZ_URL}/tokens",
            group="other",
            affects_status=False,
        )
        results.append(
            _endpoint_result(
                token_spec, None, "missing session_id", site_id=site_id, serial=serial
            )
        )

    # Headers for authenticated requests.
    base_headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    if site_id:
        base_headers["Referer"] = f"{BASE_URL}/pv/systems/{site_id}/summary"
    else:
        base_headers["Referer"] = f"{BASE_URL}/"
    if cookie_header:
        base_headers["Cookie"] = cookie_header
    if xsrf:
        base_headers["X-CSRF-Token"] = xsrf
    if eauth:
        base_headers["e-auth-token"] = eauth

    bearer = _extract_bearer(cookie_map) or eauth
    if bearer:
        base_headers["Authorization"] = f"Bearer {bearer}"
    history_bearer = eauth or _extract_bearer(cookie_map)
    user_id = _jwt_user_id(history_bearer)
    request_id = str(uuid.uuid4())

    # Site discovery (search endpoint)
    discovery_urls = (SITE_SEARCH_URL,)
    discovery_sites: list[str] = []
    for idx, url in enumerate(discovery_urls, start=1):
        spec = EndpointSpec(
            name=f"site_discovery_{idx}",
            method="GET",
            url=url,
            group="other",
            headers=dict(base_headers),
        )
        status_code, _headers, body, error = _request(
            opener, spec.method, spec.url, headers=spec.headers, timeout=args.timeout
        )
        results.append(
            _endpoint_result(spec, status_code, error, site_id=site_id, serial=serial)
        )
        if status_code == 200 and not discovery_sites:
            payload = _parse_json(body)
            discovery_sites = _normalize_sites(payload)

    if not site_id and discovery_sites:
        site_id = discovery_sites[0]

    if not site_id:
        status = "Down"
        status_details = _evaluate_status(results)
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Site discovery failed",
            "summary": status_details[1]["summary"],
            "checks": status_details[1]["checks"],
            "endpoints": results,
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    # Update headers now we have a site id
    base_headers["Referer"] = f"{BASE_URL}/pv/systems/{site_id}/summary"

    control_headers = dict(base_headers)

    history_headers = dict(control_headers)
    if history_bearer:
        history_headers["Authorization"] = f"Bearer {history_bearer}"
    history_session_id = _jwt_session_id(history_bearer)
    if history_session_id:
        history_headers["e-auth-token"] = history_session_id
    else:
        history_headers.pop("e-auth-token", None)
    if request_id:
        history_headers["requestid"] = request_id
    if user_id:
        history_headers["username"] = user_id

    # Charger summary (also used for serial discovery)
    summary_url = (
        f"{BASE_URL}/service/evse_controller/api/v2/{site_id}/ev_chargers/summary"
        f"?filter_retired=true"
    )
    summary_spec = EndpointSpec(
        name="charger_summary_v2",
        method="GET",
        url=summary_url,
        group="other",
        headers=dict(base_headers),
    )
    status_code, _headers, body, error = _request(
        opener, summary_spec.method, summary_spec.url, headers=summary_spec.headers, timeout=args.timeout
    )
    results.append(
        _endpoint_result(
            summary_spec, status_code, error, site_id=site_id, serial=serial
        )
    )
    if not serial and status_code == 200:
        payload = _parse_json(body)
        chargers = _normalize_chargers(payload)
        if chargers:
            serial = chargers[0]

    # Main status endpoint (control-plane health)
    status_url = f"{BASE_URL}/service/evse_controller/{site_id}/ev_chargers/status"
    status_spec = EndpointSpec(
        name="charger_status",
        method="GET",
        url=status_url,
        group="main",
        headers=dict(control_headers),
    )
    status_code, _headers, body, error = _request(
        opener, status_spec.method, status_spec.url, headers=status_spec.headers, timeout=args.timeout
    )
    results.append(
        _endpoint_result(status_spec, status_code, error, site_id=site_id, serial=serial)
    )
    if not serial and status_code == 200:
        payload = _parse_json(body)
        chargers = _normalize_chargers(
            (payload or {}).get("evChargerData") if isinstance(payload, dict) else payload
        )
        if chargers:
            serial = chargers[0]

    if not serial:
        status = "Down"
        status_details = _evaluate_status(results)
        payload = {
            "status": status,
            "checked_at": started_at,
            "reason": "Serial discovery failed",
            "summary": status_details[1]["summary"],
            "checks": status_details[1]["checks"],
            "endpoints": results,
        }
        _write_outputs(args.output_dir, status=status, payload=payload)
        return 0

    # Degraded-service endpoints
    today = datetime.now().strftime("%d-%m-%Y")
    now_utc = datetime.now(timezone.utc)
    yesterday_utc = now_utc - timedelta(days=1)
    inverter_date_query = {
        "start_date": yesterday_utc.strftime("%Y-%m-%d"),
        "end_date": now_utc.strftime("%Y-%m-%d"),
    }
    criteria_query = {"source": "evse", "requestId": request_id}
    if user_id:
        criteria_query["username"] = user_id
    criteria_url = (
        f"{BASE_URL}/service/enho_historical_events_ms/{site_id}/filter_criteria"
        f"?{urllib.parse.urlencode(criteria_query)}"
    )
    battery_common_params: dict[str, str] = {}
    if user_id:
        battery_common_params["userId"] = user_id
    battery_profile_params = dict(battery_common_params)
    battery_profile_params["source"] = "enho"
    if locale:
        battery_profile_params["locale"] = locale
    battery_settings_params = dict(battery_common_params)
    battery_settings_params["source"] = "enho"

    battery_headers = dict(control_headers)
    if user_id:
        battery_headers["Username"] = user_id
    battery_headers["Origin"] = "https://battery-profile-ui.enphaseenergy.com"
    battery_headers["Referer"] = "https://battery-profile-ui.enphaseenergy.com/"

    degraded_specs = [
        EndpointSpec(
            name="scheduler_charge_mode",
            method="GET",
            url=(
                f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
                f"{site_id}/{serial}/preference"
            ),
            group="degraded",
            headers=dict(control_headers),
        ),
        EndpointSpec(
            name="scheduler_green_settings",
            method="GET",
            url=(
                f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
                f"GREEN_CHARGING/{site_id}/{serial}/settings"
            ),
            group="degraded",
            headers=dict(control_headers),
        ),
        EndpointSpec(
            name="scheduler_schedules",
            method="GET",
            url=(
                f"{BASE_URL}/service/evse_scheduler/api/v1/iqevc/charging-mode/"
                f"SCHEDULED_CHARGING/{site_id}/{serial}/schedules"
            ),
            group="degraded",
            headers=dict(control_headers),
        ),
        EndpointSpec(
            name="session_history_criteria",
            method="GET",
            url=criteria_url,
            group="degraded",
            headers=dict(history_headers),
        ),
        EndpointSpec(
            name="session_history",
            method="POST",
            url=f"{BASE_URL}/service/enho_historical_events_ms/{site_id}/sessions/{serial}/history",
            group="degraded",
            headers=dict(history_headers),
            json_body={
                "source": "evse",
                "params": {
                    "offset": 0,
                    "limit": 1,
                    "startDate": today,
                    "endDate": today,
                    "timezone": "UTC",
                },
            },
        ),
        EndpointSpec(
            name="site_energy",
            method="GET",
            url=f"{BASE_URL}/pv/systems/{site_id}/lifetime_energy",
            group="degraded",
            headers=dict(base_headers),
        ),
        EndpointSpec(
            name="battery_site_settings",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/batteryConfig/api/v1/siteSettings/{site_id}",
                battery_common_params,
            ),
            group="degraded",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 400, 404, 422),
        ),
        EndpointSpec(
            name="battery_profile",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/batteryConfig/api/v1/profile/{site_id}",
                battery_profile_params,
            ),
            group="degraded",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 400, 404, 422),
        ),
        EndpointSpec(
            name="battery_settings",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{site_id}",
                battery_settings_params,
            ),
            group="degraded",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 400, 404, 422),
        ),
        EndpointSpec(
            name="battery_storm_guard_alert",
            method="GET",
            url=f"{BASE_URL}/service/batteryConfig/api/v1/stormGuard/{site_id}/stormAlert",
            group="degraded",
            headers=dict(battery_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="devices_inventory",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/devices.json",
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="grid_control_check",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/grid_control_check.json",
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="battery_backup_history",
            method="GET",
            url=f"{BASE_URL}/app-api/{site_id}/battery_backup_history.json",
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="battery_status",
            method="GET",
            url=f"{BASE_URL}/pv/settings/{site_id}/battery_status.json",
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="inverters_inventory",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/app-api/{site_id}/inverters.json",
                {"limit": 1000, "offset": 0, "search": ""},
            ),
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="inverter_status",
            method="GET",
            url=f"{BASE_URL}/systems/{site_id}/inverter_status_x.json",
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="inverter_production",
            method="GET",
            url=_with_query(
                f"{BASE_URL}/systems/{site_id}/inverter_data_x/energy.json",
                inverter_date_query,
            ),
            group="degraded",
            headers=dict(base_headers),
            affects_status=False,
            ok_statuses=(200, 404),
        ),
        EndpointSpec(
            name="auth_settings",
            method="POST",
            url=(
                f"{BASE_URL}/service/evse_controller/api/v1/{site_id}/"
                f"ev_chargers/{serial}/ev_charger_config"
            ),
            group="degraded",
            headers=dict(control_headers),
            json_body=[
                {"key": "rfidSessionAuthentication"},
                {"key": "sessionAuthentication"},
            ],
        ),
    ]

    for spec in degraded_specs:
        status_code, _headers, _body, error = _request(
            opener,
            spec.method,
            spec.url,
            headers=spec.headers,
            json_body=spec.json_body,
            timeout=args.timeout,
        )
        results.append(
            _endpoint_result(spec, status_code, error, site_id=site_id, serial=serial)
        )

    status, details = _evaluate_status(results)
    payload = {
        "status": status,
        "checked_at": started_at,
        "summary": details["summary"],
        "checks": details["checks"],
        "endpoints": results,
    }
    _write_outputs(args.output_dir, status=status, payload=payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
