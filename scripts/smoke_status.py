from __future__ import annotations

import asyncio
import json
import os
import webbrowser
from typing import Any

import aiohttp
import sys
import pathlib

# Ensure repo root is on sys.path when running from scripts/
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main() -> None:
    site_id = os.environ.get("SITE_ID")
    eauth = os.environ.get("EAUTH") or os.environ.get("E_AUTH_TOKEN")
    cookie = os.environ.get("COOKIE")
    curl = os.environ.get("CURL")

    if not site_id or not eauth or not cookie:
        print("Missing env. Set SITE_ID, EAUTH (or E_AUTH_TOKEN) and COOKIE.")
        print("Opening Enlighten login page in your browser so you can sign in and copy headers...")
        try:
            webbrowser.open("https://enlighten.enphaseenergy.com/")
        except Exception:
            pass
        site_id = site_id or input("Enter Site ID (or leave blank and paste cURL below): ").strip()
        # Optional: allow paste of 'Copy as cURL' to auto-extract headers and site
        curl = curl or input("Paste 'Copy as cURL' (optional, press Enter to skip): ").strip()
        if curl:
            site_id2, eauth2, cookie2 = _parse_curl(curl)
            site_id = site_id or site_id2
            eauth = eauth or eauth2
            cookie = cookie or cookie2
        if not eauth:
            eauth = input("Paste e-auth-token: ").strip()
        if not cookie:
            cookie = input("Paste Cookie header: ").strip()
        # Cleanup Site ID (strip stray characters like ')', spaces)
        site_id = "".join(ch for ch in site_id if ch.isdigit())
        if not (site_id and eauth and cookie):
            print("Still missing required inputs.")
            return

    # Load api.py with package context so relative imports work
    import importlib.util
    import types
    pkg_dir = ROOT / "custom_components" / "enphase_ev"
    pkg_name = "custom_components.enphase_ev"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(pkg_dir)]
    sys.modules[pkg_name] = pkg
    # const
    const_spec = importlib.util.spec_from_file_location(f"{pkg_name}.const", str(pkg_dir / "const.py"))
    const_mod = importlib.util.module_from_spec(const_spec)
    assert const_spec and const_spec.loader
    sys.modules[f"{pkg_name}.const"] = const_mod
    const_spec.loader.exec_module(const_mod)  # type: ignore[attr-defined]
    # api
    api_spec = importlib.util.spec_from_file_location(f"{pkg_name}.api", str(pkg_dir / "api.py"))
    api_mod = importlib.util.module_from_spec(api_spec)
    assert api_spec and api_spec.loader
    sys.modules[f"{pkg_name}.api"] = api_mod
    api_spec.loader.exec_module(api_mod)  # type: ignore[attr-defined]
    EnphaseEVClient = getattr(api_mod, "EnphaseEVClient")
    Unauthorized = getattr(api_mod, "Unauthorized")

    async with aiohttp.ClientSession() as session:
        client = EnphaseEVClient(session, site_id, eauth, cookie)
        try:
            data: dict[str, Any] = await client.status()
        except Unauthorized:
            print("401 Unauthorized: Refresh e-auth-token and Cookie from an active session.")
            return
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")
            return

    chargers = (data.get("evChargerData") or [])
    print(f"Site {site_id}: {len(chargers)} charger(s)")
    if not chargers:
        print("Raw response:")
        print(json.dumps(data, indent=2))
    for obj in chargers:
        sn = obj.get("sn")
        name = obj.get("name")
        plugged = obj.get("pluggedIn")
        charging = obj.get("charging")
        status = obj.get("connectorStatusType")
        kwh = (obj.get("session_d") or {}).get("e_c")
        print(f"- {sn} | {name} | plugged={plugged} charging={charging} status={status} session_kwh={kwh}")


def _parse_curl(curl: str):
    import re
    from urllib.parse import urlparse
    try:
        m_url = re.search(r"curl\s+'([^']+)'|curl\s+\"([^\"]+)\"|curl\s+(https?://\S+)", curl)
        url = next(g for g in (m_url.group(1) if m_url else None, m_url.group(2) if m_url else None, m_url.group(3) if m_url else None) if g)  # type: ignore
        headers = {}
        for m in re.finditer(r"-H\s+'([^:]+):\s*([^']*)'|-H\s+\"([^:]+):\s*([^\"]*)\"", curl):
            key = m.group(1) or m.group(3)
            val = m.group(2) or m.group(4)
            if key and val:
                headers[key.strip()] = val.strip()
        eauth = headers.get("e-auth-token")
        cookie = headers.get("Cookie")
        path = urlparse(url).path
        m_site = re.search(r"/evse_controller/(\d+)/", path) or re.search(r"/pv/systems/(\d+)/", path)
        site_id = m_site.group(1) if m_site else None
        return site_id, eauth, cookie
    except Exception:
        return None, None, None


if __name__ == "__main__":
    asyncio.run(main())
