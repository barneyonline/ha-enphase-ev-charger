# Envoy History Migration

The integration now includes an optional `Migrate Envoy history` assistant in the Options flow.

Use it when you want Enphase Energy site totals to reuse existing `enphase_envoy` Energy-dashboard entity IDs so long-term Home Assistant statistics continue under this integration.

## Before you start

- Create a full Home Assistant backup.
- Confirm the Enphase Energy integration is already set up and its site energy sensors are available.
- Keep the old Enphase Envoy entities in the entity registry.
- If the Envoy integration is already unloaded, the assistant can still discover compatible Envoy sensors from existing recorder statistics.

## How it works

The assistant:

1. Lets you pick an Envoy source entry when more than one is present.
2. Suggests likely Envoy-to-Enphase mappings for compatible cumulative energy totals.
3. Validates that the selected sensors are compatible energy totals and that the Enphase Energy value is not lower than the Envoy value.
4. Temporarily unloads the selected Envoy integration entry while the migration runs.
5. Renames the selected Envoy energy entities to archived legacy `entity_id` values.
6. Renames the Enphase Energy entities to the original Envoy `entity_id` values.
7. Reloads Enphase Energy and restores the Envoy integration so non-migrated Envoy entities remain available.

Archived Envoy energy sensors are disabled by default so they do not conflict with the migrated Enphase Energy sensors. The migration remains explicit and optional. Blank mapping fields are skipped.

## Recommended checks after migration

- Open the Home Assistant Energy dashboard and confirm the expected entities are still selected.
- Check Developer Tools -> Statistics for any statistics warnings.
- Verify the Enphase Energy entities now own the migrated `entity_id` values.
- Confirm any remaining non-migrated Envoy entities are available again after the reload.

## Rollback

Restoring the Home Assistant backup taken before migration is the safest rollback path.
