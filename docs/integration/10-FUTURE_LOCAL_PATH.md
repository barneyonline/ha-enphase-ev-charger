# Future Local Path (owner / installer)

- Local endpoints discovered (but role-gated for owner): `/ivp/pdm/*`, `/ivp/peb/*`
- If Enphase exposes owner access in a future firmware, a local client can be added:
  - Probe: try `/ivp/pdm/charger/<sn>/status`
  - If 200 JSON, prefer local client over cloud
  - Fallback: current cloud client
- Optional: detect an **installer** token/cookie and enable local EV endpoints when present.
