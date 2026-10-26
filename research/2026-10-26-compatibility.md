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
