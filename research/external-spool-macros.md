# External-spool macros — 2026-10-26

AFC is reserved for AMS integration. The external spool uses `LOAD_EXTERNAL_FILAMENT`, `UNLOAD_EXTERNAL_FILAMENT`, and `SET_EXTERNAL_FILAMENT`, exposed as virtual Klipper macros with parameter metadata for the existing Mainsail/Fluidd macro controls.

## Primary references

- [Bambuddy MQTT service](https://github.com/maziggy/bambuddy/blob/main/backend/app/services/bambu_mqtt.py), `ams_load_filament`, `ams_unload_filament`, `ams_set_filament_setting`: external load uses `ams_change_filament`, AMS 255, slot/target 254; unload uses slot/target 255 and nozzle temperatures. Settings use `ams_filament_setting`, AMS 255, **global tray 254**, slot 0. Uppercase colour is significant on P1S firmware. These functions also document why dual-nozzle routing needs separate handling.
- [BambuStudio generic PLA profile](https://github.com/bambulab/BambuStudio/blob/master/resources/profiles/BBL/filament/Generic%20PLA%20%40base.json) and corresponding Generic ABS/PETG/ASA/TPU/PC/PA base profiles: generic filament IDs. Temperature defaults in this shim are editable metadata, not copied or claimed as universal manufacturer settings.
- [Mainsail v2.17 macro button](https://github.com/mainsail-crew/mainsail/blob/v2.17.0/src/components/inputs/MacroButton.vue), [printer getters](https://github.com/mainsail-crew/mainsail/blob/v2.17.0/src/store/printer/getters.ts), and [parameter parser](https://github.com/mainsail-crew/mainsail/blob/v2.17.0/src/plugins/helpers.ts): requires macro status objects, lowercase config sections with `gcode` discovery templates containing `params.*`, and command help entries.
- [Snapmaker AFC-Lite precedent](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/afc-lite.md): reviewed as an example of adapting a native printer interface to an existing UI. No AFC objects are exposed for the external spool.

## Verification boundaries

Update: the operator has now verified external unload and initial load on the P1S. The initial load exposed the missing native **Has filament extruded?** response. Added `EXTERNAL_FILAMENT_EXTRUDED` (`ams_control`, `param=done`) and `EXTERNAL_FILAMENT_RETRY` (`param=resume`), following Bambuddy's HMS action dispatcher. The P1S action catalog maps errors `07FF8007` and `07FFC00A` to `FILAMENT_EXTRUDED` / `RETRY_FILAMENT_EXTRUDED`. These buttons require that reported question, idle state, and confirmed external selection (current or target tray 254); they never answer an active print or selected AMS tray. Command echoes cannot create the question. The question remains pending until firmware telemetry clears it. No automatic modal is implemented yet; both choices appear as ordinary macros. Responses are mock tested but not yet physically verified.

References: [Bambuddy HMS action catalog](https://github.com/maziggy/bambuddy/blob/main/backend/app/data/hms_actions.json), [action dispatch](https://github.com/maziggy/bambuddy/blob/main/backend/app/services/bambu_mqtt.py). The original implementation/deployment record below predates these operator tests.

All 120 tests passed locally and in the new image with networking disabled. Deployed to `fakeraker` after observing inactive state; verified backup `.backups/2026-10-26-extrusion-choice`, previous container retained stopped as `fakeraker-before-extrusion-choice-20261026`. MQTT reconnected successfully. Read-only API verification confirmed `awaiting_extrusion_confirmation=true` on the real printer, external loaded telemetry, and both new buttons in the existing Mainsail group. Current reported material is now PLA (`GFL99`, 190–240°C); the shim did not change it during this work. Neither response button was invoked on the printer during implementation. Refresh Mainsail to load the new buttons.

ABS is currently loaded according to the user. No load, unload, material-setting, or heater command has been sent to the real printer while implementing this feature. Use mocked transport and an isolated mock container for validation.

A delivered MQTT packet is only a requested action. The shim awaits transport delivery for these new commands, surfaces failures without automatically replaying physical operations, and updates loaded status only from telemetry. Existing heater command variants and their delivery behavior are unchanged.

The future print test uses `~/Downloads/3DBenchy_PLA_24m11s.gcode.3mf`. It exists locally but has not been uploaded or started. The test remains on hold until the user is present and has loaded PLA. Do not switch the printer's external-spool metadata to PLA merely because that is the planned test material.

## Result

70 tests passed locally and in the final Docker image with network disabled. The Mainsail mock UI displayed all three buttons, discovered the material/colour/range/profile parameter form, saved ABS/colour, and exercised load/unload entirely in mock mode. The tested image was deployed to `fakeraker` only after observing an inactive print state, with a verified `.backups/2026-10-26-external/` data backup and stopped rollback container `fakeraker-before-external-20261026`.

Read-only telemetry on the deployed shim confirmed external `loaded=true`, `material=ABS`, `color=76D9F4FF`, `profile_id=GFB60`, and nozzle range 240–280°C. No material edits or load/unload commands were sent to the real printer. The reported profile is preserved for colour-only edits; a material change selects the matching generic profile unless explicitly overridden.

The user's live Mainsail was configured for expert macro groups with its old group hidden. A separate External Spool group was installed using only database APIs, visible while inactive. A comparison against the pre-deployment backup verified every existing macro group and dashboard entry was preserved. The live database still has no saved `bambu_shim.external_filament` request; the ABS values came entirely from printer telemetry.

## One-click material presets

Added `SET_PLA`, `SET_PETG`, `SET_ABS`, `SET_ASA`, `SET_TPU`, `SET_PC`, and `SET_PA`. Each explicitly selects the material's generic profile and default nozzle range, including when resetting a custom profile for the same material, and preserves the existing reported/saved colour. The buttons take no parameters and do not heat or load filament. `SET_EXTERNAL_FILAMENT` remains available for custom settings.

72 tests passed locally and in the final Docker image with network disabled, covering each preset's payload, same-material default reset, colour preservation, UI discovery, and invalid arguments. The local shim and existing External Spool group were updated. Read-only telemetry remained loaded ABS, colour `76D9F4FF`, profile `GFB60`, nozzle range 240–280°C, with nozzle target off. No preset was invoked on the real printer. Pre-update data is backed up in `.backups/2026-10-26-presets/`; `fakeraker-before-presets-20261026` is retained stopped for rollback.
