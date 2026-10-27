# Compatibility review — 2026-10-26

## Where work stopped

Latest commit: `969dd42` (March 22), adding print pause/resume/cancel. The preceding work added live job timing/metadata (`4f14f19`), a shared P1 camera stream (`a16ef2d`), and Fluidd/nested settings views (`6cd2d13`). The deployed Docker container runs MQTT and the P1 camera successfully. The README had lagged behind the camera implementation.

## References worth following

- [Bambuddy](https://github.com/maziggy/bambuddy): the strongest adjacent implementation for printer lifecycle, file transfer, cameras, and print management. Its [MQTT service](https://github.com/maziggy/bambuddy/blob/main/backend/app/services/bambu_mqtt.py) requests `pushall` and firmware information on connect, accounts for delta reports, and handles missing/invalid telemetry. Its [camera fan-out service](https://github.com/maziggy/bambuddy/blob/main/backend/app/services/camera_fanout.py) shares one upstream connection and drops stale queued frames for slow viewers. Our camera manager already follows the single-upstream pattern. Study the state and job lifecycle before extending our current history implementation; don't copy an entire application's architecture into a shim.
- [Moonraker database API](https://moonraker.readthedocs.io/en/latest/external_api/database/): source of truth for namespace/item behavior, dotted versus array keys, missing-item errors, delete return values, and protected namespaces. Compatibility should be tested over both HTTP and JSON-RPC, not inferred from a successful UI connection.
- [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI): protocol reference for MQTT payloads, printer model differences, camera protocols, and FTPS. Community documentation needs checking against model/firmware behavior.
- [Home Assistant Bambu integration](https://github.com/greghesp/ha-bambulab): another useful source for telemetry modeling and feature detection across printer models. Compare reported capabilities instead of assuming every printer has the same hardware.

## Confirmed faults addressed

- Whole-file JSON settings rewrites with swallowed save errors: replaced by transactional SQLite namespace storage in the existing mounted `bambu_shim.db`. Legacy JSON is imported once, with dotted Mainsail/Fluidd keys normalized, and preserved byte-for-byte.
- Inconsistent nested-key behavior, array-key crashes, false success for missing items, incorrect delete values, and unrestricted/missing parameters: shared database implementation and transport handling with regression tests.
- Maintenance writes silently ignored over RPC: persisted normally.
- Fluidd never initialized the webcam component because the server advertised `webcams` instead of Moonraker’s `webcam` component name: corrected the advertised component and added the missing API-version string.
- Built-in camera settings silently ignored: name, rotation, flips, FPS, location, and enabled preference persist; built-in transport URLs remain tied to the camera bridge.
- Standard HTTP object queries returned empty results: support `?extruder=temperature,target&print_stats`, JSON object maps, and field filtering shared with RPC.
- Malformed RPC could terminate a connection; unknown methods reported success: explicit parse/request/parameter/method errors and notification/batch handling.
- Reconnect didn't request a full MQTT status snapshot: request `pushall` and firmware information after subscription; cleanly cancel the MQTT task on shutdown.
- PREPARE/SLICING and FAILED mapped to standby: map them to printing and error.
- Uploads exceeded Nginx’s default 1 MiB limit, multipart print flags were ignored, and the `print` argument shadowed error logging: remove the proxy size limit, consume form flags, and return errors for unsupported roots/subdirectory uploads.
- History pagination ignored `start`, resume replaced the active job ID/start, completion cleared timing, and Fluidd console help was missing: preserve the job across pause/resume, freeze terminal durations, paginate queries, and expose command help.
- SQL cache/history tests wrote into the runtime database: isolate their stores and assert results. Move manual hardware LED scripts out of automated test discovery.

## Remaining limits requiring explicit follow-through

- Real heater, motion, pause/resume/cancel, upload/print-start acceptance needs a controlled printer test. Wait until the user is physically in the room before print tests, and preserve the established wait-versus-non-wait heater command choices unless concrete evidence warrants a change. A successful MQTT publish is not proof the printer accepted a command. Publishing currently happens in background tasks, so command delivery errors do not reliably propagate to the caller.
- Job history records terminal state transitions, but an in-flight job is not durable across a shim restart. Some metadata remains synthetic (size, filament, layer-height fallbacks). This must not be presented as accurate historical accounting.
- HTTP coverage still trails WebSocket coverage, including some printer commands and webcams. Database backup/restore endpoints are not implemented. The SQLite file schema is shim-specific, not native Moonraker's schema.
- WebSocket status subscriptions are still global rather than per-client filters.
- P1-style camera support is not RTSP support for all models.
- Full Moonraker parity, custom Klipper macros, and job queuing remain outside the current implementation.

The initial verification should concentrate on settings round trips, restart durability, unchanged legacy JSON, Mainsail/Fluidd startup, temperature observation, and camera snapshots. Hardware actuation needs its own observed test rather than a broad claim that every control works.

## Verification and deployment

- 54 automated tests passed locally and in the final Docker image with network disabled.
- Live HTTP/WebSocket database smoke checks passed against the deployed `fakeraker` container, including malformed-message recovery.
- A printer-name change saved through Fluidd persisted in SQLite and survived a real container restart and browser reload. The original `Fakeraker` name was restored afterward.
- Original `moonraker.json` content hash remained unchanged; SQLite `PRAGMA integrity_check` returned `ok`.
- All three UI HTTP servers returned 200. Mainsail showed live temperatures/video; Fluidd initialized printer state, thermals, console help, and webcams. The P1 camera snapshot endpoint returned 200.
- A >2 MiB upload and multipart upload-and-print flag were verified only in the disposable mock container, without a printer connection.
- The final image is tagged `bambu-moonraker-shim` and deployed as `fakeraker` with the prior environment, mounts, and ports.
- Verified local data backups are in `.backups/2026-10-26/` and `.backups/2026-10-26-final/`, ignored by Git and Docker builds. The stopped original `fakeraker-before-20261026` and intermediate `fakeraker-before-final-20261026` containers remain available for rollback. Automatic approval review blocked intermediate-container cleanup, so both were retained.

## File deletion follow-up

Live logs showed Mainsail attempted `server.files.delete_file` for `gcodes/cube3.gcode` and `gcodes/cube2.gcode`. Both failed with method-not-found: only HTTP deletion had been implemented. Added a shared HTTP/WebSocket delete operation with Moonraker's `item`/`action` response and `notify_filelist_changed` notification. Blocking FTPS calls run off the event loop; successful deletion invalidates the cache. Missing files, invalid roots/paths, active prints, and FTPS errors return explicit failures. Legacy display aliases are resolved from a fresh parent listing with exact filenames preferred and original case preserved.

Regression tests use disposable local files or mocked FTPS, and `scripts/validation/smoke_file_delete.py` refuses non-mock mode. No real printer files were deleted during development or verification; the user can retry their deletion after deployment.

### Native 3MF files in Mainsail

Mainsail v2.17.0's `validGcodeExtensions` (`src/store/variables.ts`) excludes `.3mf`; its file getter, upload classifier and row actions share that list. Docker now builds the checksum-pinned upstream source with `docker/mainsail/3mf-support.patch`, adding `.3mf`, case-insensitive extension checks and metadata refresh support. Archive files retain print/download/delete actions, while plain-text editing and the plain G-code viewer are disabled for 3MF archives. Source builds regenerate hashed assets and the service worker.

The shim now returns real filenames in HTTP/RPC lists and directories. Legacy alias resolution remains for older browser tabs, with exact filenames preferred to prevent collisions between `cube.gcode` and `cube.gcode.3mf`. All 86 tests pass locally and in the image without networking. Mock browser verification showed plain `.gcode`, `.gcode.3mf`, `.3mf` and uppercase extensions simultaneously, and the print confirmation retained `cube.gcode.3mf`; it was cancelled without starting a print. Physical print tests remain pending.

### Mainsail 2.19.0 upgrade

Updated the default source version/checksum to v2.19.0 and rebased the 3MF patch against that tag without fuzzy hunks. The Mainsail build stage uses Node 22 to satisfy the updated build dependencies; the CNC build stage is unchanged.

Upgrade validation: all 86 image tests passed with networking disabled; mock upload/WS delete/list-notification checks passed; browser confirmed `.gcode`, `.gcode.3mf`, `.3mf` and uppercase archive variants. Deployed service reports Mainsail v2.19.0 and shim ready, with verified backup in `.backups/2026-10-26-mainsail219` and the prior container retained stopped. No physical printer operations were tested.

### Stored-file metadata and previews

Replaced the file metadata API's live-job/synthetic response with a bounded, read-only parser for Bambu/Orca sliced 3MF plates and common Bambu/Orca/Prusa/Cura plain G-code comments. HTTP/RPC metadata, forced metascan and thumbnail-detail APIs share the implementation. Missing fields are omitted. The old live-metadata response generator and invented size/filament defaults were removed; live filament use derived from progress remains an estimate.

Metadata is cached by filename, selected plate, parser version, source size and modification time in SQLite, with preview BLOBs in the same mounted database. Generated `.thumbs/` URLs are served by the shim and never uploaded to the printer. Original PNG/JPEG previews are retained; a 64px padded preview fills Mainsail's list-icon requirement. Upload/delete invalidate parsed records and prune orphan previews. No archive paths are extracted. File/member/image limits, XML declaration rejection, source-change checks and single-flight async reads bound parsing and avoid blocking the event loop. FTPS timestamps now use UTC or zero for unknown, rather than fetch time; unknown timestamps trigger a five-minute re-read and forced scans always bypass the cache.

The supplied local Benchy archive parsed as 478,227 bytes, OrcaSlicer 2.4.2, 1,451 seconds, 192 layers, 48mm height, 0.25mm layers, 3,589.84mm filament and 10.88g, with 128px/512px original plate previews. The full mock upload/HTTP/WS metadata, thumbnail download, forced scan and missing-file flow passed without a print request. Automated coverage includes multiple plates, malformed/oversized archives, embedded G-code previews, cache restart/replacement/concurrency, path rejection and mocked FTPS failures. Physical print tests remain pending.

Final metadata verification: 109 tests pass locally, including native BambuStudio header precision and JPEG preview handling. The real stored Bambu Cube file returned a 204,139-byte source, 1,487-second estimate, 30mm height, 0.2mm layers and preview downloads over read-only FTPS. Its native G-code header reports 3,088.43mm of filament, which takes precedence over XML rounded to 3.09m. Unknown modification dates render as `--` in patched Mainsail. No printer file was uploaded/deleted and no physical command was issued during live verification. The supplied Benchy remained confined to local/mock validation.

Cold live extraction also exposed a pre-existing event-loop blocking disk-usage query in HTTP/RPC directory listings. It now runs in a worker, with a thread-affinity regression test. Unknown-timestamp FTPS reads avoid redundant full NLST/SIZE re-listings after every transfer; five-minute/forced revalidation and download-size bounds remain. The final suite contains 110 tests.

Deployed final metadata image after all 110 image tests passed with networking disabled. The final service reused the persistent cube metadata/thumbnail cache and reported ready. Verified backup: `.backups/2026-10-26-metadata-ready`; prior container retained stopped as `fakeraker-before-metadata-ready-20261026`. Mock validation container removed.

Numbered single-plate exports in the live file set required automatic metadata selection: an omitted plate now chooses plate 1 if present, otherwise the first available sliced plate; explicit missing plates still fail. The selected plate is returned in metadata. Print-start plate selection is unchanged and documented separately. Two additional regression tests bring the final suite to 112.

Final deployment passed all 112 image tests with networking disabled. Read-only verification confirmed `glass_top_riser_PLA.gcode.3mf` selects its sole plate 2 (1,455 seconds), and `Z-axis table dust cover_plate_4.gcode.3mf` selects plate 4 (2,766 seconds), each with three preview sizes. The final backup is `.backups/2026-10-26-metadata-plates` and the stopped rollback container is `fakeraker-before-metadata-plates-20261026`.

### Nonblocking heater investigation and observed bed test

Keep the existing M109/M190/M191 workaround intact: it resulted from earlier hardware debugging, and finding those opcodes in the shim does not explain why nonblocking commands failed. Historical commits changed several transport details together, so they do not isolate a cause.

The connected Panda Touch at 192.168.2.233 successfully set this P1S bed to 45°C and back to zero. The shim's normal report subscription observed successful `gcode_line` acknowledgements echoing `M140 S45\n` and `M140 S0\n`. This confirms the G-code text, but does not reveal Panda's complete request envelope, publish QoS, or MQTT client identity. Subscribing to the printer request topic timed out/disconnected; report-topic subscriptions work. Panda's HTTP root timed out during read-only inspection.

With the operator's explicit approval, `scripts/diagnostics/probe_nonblocking_bed.py` attempted the same 45°C → off test through a separate MQTT connection: literal newline-terminated M140 commands, QoS 0, sequence IDs 2005/2006, no root `user_id`, bypassing shim translation. An initial attempt refused to actuate because the bed was above the baseline threshold. The retry began at 41.0625°C, target zero, native state FINISH. The 45°C command received no acknowledgement or target change within ten seconds; the diagnostic then sent the zero-target cleanup command. The normal shim listener also recorded no acknowledgement for those probe commands. Subsequent live observation showed target zero and cooling from 40.5°C to 37.28125°C. No motion or print command was issued.

This is a failed test of that exact minimal MQTT delivery path, not evidence that M140 cannot work on the printer. The unresolved difference lies in transport/request details or printer-side command handling; no production heater behavior was changed. Raw local experiment output: `/private/tmp/bambu-direct-bed-probe.jsonl` (temporary, not a durable repository artifact). Four mock-only probe tests bring the local suite to 116 tests; the deployed production image remains the 112-test metadata build. External-spool unload was separately reported successful by the operator with ABS loaded; load and print start remain unverified.

### First physical print-start attempts, October 26–27

The operator subsequently verified initial external load and both extrusion-confirmation macro buttons, then authorized printing the supplied PLA Benchy. The printer reports external PLA loaded and no pending extrusion question. Uploaded `3DBenchy_PLA_24m11s.gcode.3mf` (478,227 bytes) to printer storage. Read it back through the file download API: SHA-256 `d5fcb7996a90e2a124463da6bd6cac09c649dce85be51102ac16ce18a5b9cd8b` matches the original. Plate 1 targets Bambu Lab P1S, 0.4mm, PLA only, 55°C bed, 220°C first-layer nozzle / 230°C later, 1,451-second estimate. No print has actually started.

The initial start API returned success merely after scheduling MQTT publication, while the printer retained its old completed-job state and did not acknowledge `project_file`. Updated job identity fields from hardcoded zero to fresh nonzero signed-int32 values, changed AMS mapping serialization to native arrays (legacy CSV input still accepted), corrected `bed_leveling`, and omitted the unrelated `cfg` field. Submission now requires confirmed idle telemetry and excludes concurrent pending submissions. A dedicated one-shot publisher waits for a correlated firmware acknowledgement and returns explicit timeout/rejection errors, never replaying a physical start on broker timeout. Command acknowledgements no longer flow into live printer telemetry.

Observed trials: corrected payload with QoS 1 and no root user ID; QoS 0 alone; QoS 0 with the shared user-ID envelope; then P1S `file:///sdcard/...` instead of the malformed `ftp://filename` reference. None produced firmware acknowledgement or an actual job/temperature transition. These are failed trials, not validated fixes. Other models now use absolute `ftp:///...` references. References: [Bambuddy submission IDs and command shape](https://github.com/maziggy/bambuddy/blob/main/backend/app/services/bambu_mqtt.py), [bambu-printer-manager P1/A1 SD-card reference](https://synman.github.io/bambu-printer-manager/mqtt-protocol-reference/).

Added sanitized firmware-module logging and native error-change logging. Current OTA firmware reports `01.06.30.01` (ESP32 `01.11.33.12`, MC `00.00.29.81`, TH `00.00.09.95`); no nonzero native print error has been observed since diagnostic reconnect. Operator confirms cloud mode enabled and reports a device overload/reboot; exact device/timing remains to be clarified. Further physical start trials are held while checking that report. Latest observed bed/nozzle targets are both zero, native job remains FINISH/complete, and the shim has a live MQTT connection. Container CPU was 0.80%, memory about 79 MiB at observation; this does not establish the cause of a device reboot.

All deployments occurred while observed inactive, retained stopped prior containers, and verified SQLite backups. October 27 backups/rollback suffixes: `print-start-qos0`, `print-start-envelope`, `print-start-url`, `print-start-diagnostics`. The deployed diagnostics image passed 126 tests with networking disabled. Additional local regression checks cover echoed-command exclusion and native errors in start timeouts. No heater-command workaround was changed.

Follow-up: the operator clarified that Panda Touch was off during these October 27 attempts, asked to disregard the reboot, and confirmed that firmware 1.06 predates the later authorization controls. Do not attribute the failure to modern command signing or require a firmware/network-mode change on that basis. A named MQTT 3.1.1 session, increased in-flight ceiling (following Bambuddy), and Studio-range print sequence IDs also failed to start this file. The current session image passed 128 tests offline before deployment; backup/rollback suffix `print-start-session`.

A separate direct Paho client could not establish TLS alongside the shim. With the idle shim temporarily stopped, a clean Paho client connected, obtained fresh FINISH/zero-target/external-PLA reports, and tested one start per session, always restoring the original container afterward. `project_file` remained unacknowledged over QoS 0, then over QoS 1, with absolute FTP URL and the single locally saved owner ID nested inside `print` (the account value was not logged or saved to the repository). `gcode_file` did receive explicit `fail` / `error string` responses for the archive basename and its `/sdcard/` path. These failed comparisons do not justify an automatic legacy fallback or a change to the account login. One extra TLS handshake timing out does not establish a universal broker connection limit.

The operator then started the exact uploaded Benchy from the P1S SD-card menu. Live telemetry changed to printing/preparation with the matching filename, then bed target 55°C, nozzle target 75°C, progress 0%, and 192 total layers. This validates the uploaded archive and local printer execution, not remote MQTT start. The operator cancelled during preparation before layer 1. The shim's `stop` command received a successful firmware acknowledgement, and both heater targets changed to zero. Cancel is hardware verified. A missing broker PUBACK caused the existing generic publisher to replay the acknowledged stop; the second stop also succeeded.

Two job-state defects exposed by this test are fixed in source: new-job deltas reset the previous job's progress/layers rather than briefly displaying its final layer and 100%; an acknowledged, correlated user stop maps subsequent native FAILED to cancelled, while unrelated/rejected stops and genuine failures remain errors. Stop already acknowledged by firmware is not replayed on broker timeout. These source updates are mock tested and await deployment; the running printer/shim was not restarted during preparation or cancellation. The original short preparation run remains recorded as an error by the older deployed state mapper; historical records were not rewritten. Remote start remains unresolved, and no second Benchy was started automatically after the user's cancel.

Final reporting update: all 131 tests passed locally and in the exact image with networking disabled. Deployed after the user's cancel, requiring inactive SD and both heater targets zero before restarting only the shim. Verified backup `.backups/2026-10-27-print-cancel-reporting`; previous container retained stopped as `fakeraker-before-print-cancel-reporting-20261027`. MQTT reconnected and targets remained zero. Stop-ack correlation is session-local; restarting after the earlier cancel does not reconstruct that correlation, so the already-terminated job still displays error rather than being relabelled from inference. Future cancels during a connected session use the corrected mapping; restart-stable cancellation identity remains a limitation. No firmware/account setting was changed and no further physical start was issued after cancellation.

### Webcam identity and deletion compatibility

The operator reported five duplicate `New Webcam` entries inserted by HelixScreen that could not be deleted, and explicitly requested preserving Bambu Camera, Webcam, and V4L2. Mainsail v2.19.0 `src/store/gui/webcams/actions.ts` deletes by `{name}`, while the shim only read `{uid}` and silently returned idempotent success for an absent UID. Camera creation also discarded a supplied UID and generated a new one, and name-only saves did not update the existing entry.

The webcam API now accepts either identity: explicit UID takes precedence and deletes only that UID; a legacy name delete removes matching legacy duplicate names. Name-only saves update an unambiguous existing camera; client-supplied UIDs are preserved for repeat saves. Ambiguous name-only updates return conflict instead of creating another duplicate. New cameras require a nonempty name rather than silently inventing `New Webcam`. Protected built-in camera deletion returns an explicit error by UID or current name; editing by name retains its fixed working URLs.

All 136 tests passed locally and in the exact image with networking disabled. Deployed while inactive, with verified backup `.backups/2026-10-27-webcams` and stopped rollback container `fakeraker-before-webcams-20261027`. The live name-based deletion removed exactly the five reported duplicate entries after checking their names/URLs and saving `webcams-before-cleanup.json` in that backup folder. A fresh API list verified all three retained camera records remained identical field for field, including their URLs and enabled flags. No printer operation was issued. This fixes the shim's identity/deletion semantics; it does not correct URLs independently generated by an external HelixScreen installation.
