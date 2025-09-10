**Integration Quality Scale**

- Current level: `silver` (see `custom_components/enphase_ev/manifest.json` → `quality_scale`).
- Source: Home Assistant developer docs — Integration Quality Scale and rules (developers.home-assistant.io).

**Completed Rules**
- Config flow (`config_flow`)
- Options flow (`options_flow`)
- Inject shared web session (`inject_websession`)
- Common modules and base entities (`common_modules`)
- Use `EntityCategory` for diagnostics (`entity_category`)
- Integration owner/codeowner (`integration_owner`)
- Appropriate polling strategy (`appropriate_polling`)
- Log once when unavailable via coordinator (`log_when_unavailable`)
- Integration diagnostics (redacted) (`integration_diagnostics`)
- System Health info + translations (`system_health`)
- Icon translations (`icon_translations`)
- Documentation: high-level description (`docs_high_level_description`)
- Documentation: configuration parameters (`docs_configuration_parameters`)
- Documentation: supported functions/entities (`docs_supported_functions`)
- Documentation: data update strategy (`docs_data_update`)
- Documentation: known limitations (`docs_known_limitations`)
- Documentation: troubleshooting (`docs_troubleshooting`)

These map to the items marked `done` in `quality_scale.yaml`.

**Rules Catalog (status)**
- Config flow: done
- Options flow: done
- Inject shared web session: done
- Common modules/base entities: done
- Use `EntityCategory`: done
- Integration owner/codeowner: done
- Appropriate polling: done
- Log when unavailable (single log): done
- Integration diagnostics: done
- System Health (info + translations): done
- Icon translations: done
- Docs: high-level description: done
- Docs: configuration parameters: done
- Docs: supported functions/entities: done
- Docs: data update behavior: done
- Docs: known limitations: done
- Docs: troubleshooting: done

**Potential Next Steps Toward Higher Levels**
- Reconfiguration flow (allow changing credentials/host without re-add)
  - Rule: reconfiguration-flow (developers docs show async_step_reconfigure).
  - Status: not tracked in quality_scale.yaml.

- Discovery and update info (zeroconf, bluetooth, or MQTT discovery if applicable)
  - Rule: discovery-update-info (manifest fields like zeroconf, bluetooth, mqtt).
  - Status: not applicable for cloud-only integrations without local discovery; review feasibility.

- Devices registry richness
  - Rule: devices (populate DeviceInfo with serial, hw/sw versions, model_id when available).
  - Status: partially implemented (serial/manufacturer/model present). hw/sw/model_id: review API support.

- Dynamic devices (auto-add/remove entities as chargers appear/disappear)
  - Rule: dynamic-devices (listen to coordinator changes and add entities).
  - Status: coordinator-driven; current setup adds entities at setup-time only — consider runtime add.

- Discovery docs and removal instructions
  - Rules: docs-installation-instructions, docs-installation-parameters, docs-removal-instructions.
  - Status: largely covered in README; consider adding removal instructions explicitly to docs.

- Tests and tooling (general quality improvements)
  - Increase test coverage for flows (reauth/options/reconfigure), diagnostics redaction, and error paths.
  - Add CI (hassfest, ruff, pytest) if not already in the upstream CI.

If you want, I can extend quality_scale.yaml to include the above rules with todo/exempt and wire a checklist into this file that stays in sync.

