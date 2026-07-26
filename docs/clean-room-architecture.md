# Clean-Room Architecture Overhaul

Status: proposed research and architecture baseline
Research snapshot: 2026-07-25

> Revisit Bambu Moonraker shim as a clean-room architectural overhaul.
> Re-evaluate the current Bambu network protocol, study actively maintained
> integrations for failure-handling and compatibility patterns, then separate
> protocol transport, normalized printer state and Moonraker API emulation.
> Preserve useful discoveries from the original implementation, but do not
> preserve its structure by default.

The primary constraint is simple: **no load-bearing legacy assumptions**.

This document records behavior, confidence, unknowns, and the intended
architecture. It does not declare the current implementation to be the
specification.

## Scope And Method

This is a clean-room design exercise in the practical engineering sense:

- Treat observable printer behavior and public protocol documentation as input.
- Use other projects to identify failure modes and architectural patterns.
- Do not copy source code, internal class structures, or test implementation.
- Write independent adapters and tests against documented behavior and
  sanitized captures.
- Record the printer model, firmware, connection mode, and source for every
  protocol claim.

This distinction matters because several reviewed projects use AGPL or
documentation licenses that are not the same as this project's MIT license.
Their behavior is useful evidence; their implementation is not a code source.

### Evidence Labels

Every protocol fixture, capability rule, or compatibility workaround should
carry one of these labels:

| Label | Meaning |
| --- | --- |
| `official` | Stated by Bambu Lab or Moonraker documentation. |
| `observed` | Reproduced from sanitized traffic or hardware owned by a contributor. |
| `corroborated` | Independently reported by at least two maintained projects. |
| `community` | Present in one maintained project or community protocol document. |
| `hypothesis` | Plausible interpretation that still requires capture or hardware validation. |
| `unknown` | The behavior has not been established. |

Rules derived only from a model name or a single firmware report must never be
promoted above `community` without another source.

## Current Network Behavior

The protocol is not one stable local API. Current printers can expose different
security and transport behavior based on model, firmware, LAN mode, Developer
Mode, account binding, and printer settings.

### Discovery And Pairing

| Behavior | Current understanding | Confidence | Design consequence |
| --- | --- | --- | --- |
| Local discovery | Bambu printers advertise and answer SSDP-like traffic on UDP port `2021`. Community implementations use `urn:bambulab-com:device:3dprinter:1` and parse serial, model, name, connection mode, security mode, and firmware headers. | `corroborated` | Discovery returns candidates only. It does not imply authentication or command capability. |
| Discovery transport | Real printers have been observed using broadcast as well as SSDP multicast conventions. Containers and routed networks frequently lose these packets. | `community` | Support listener, active search, manual host configuration, and optional subnet probing as separate strategies. |
| Account binding | Bambu documents QR, SSDP, and Bluetooth binding. QR codes are time limited. P1 uses BLE for network configuration and binding. | `official` | Account binding is a distinct workflow, not MQTT login. The shim should not claim to pair unless it implements the complete binding flow. |
| Legacy/Developer local access | Local services use username `bblp` and the printer LAN access code. Developer Mode intentionally exposes MQTT, file transfer, and live view without the newer command authorization path. | `official` and `corroborated` | Represent this as a `developer_local` session profile. |
| Standard LAN authorization | Current Standard LAN Mode uses local binding. Community captures show per-printer client certificates and signed command envelopes on authorization-enabled firmware. | `official` for binding, `community` for wire details | Represent this as an `authorized_local` profile. Certificate enrollment and signing are first-class components, not flags in MQTT publish code. |
| Cloud binding | Cloud MQTT uses an account user ID and access token. The printer separately authenticates to Bambu cloud with factory credentials. | `official` and `community` | Cloud credentials and local access codes are different secret types with separate lifecycles. |

Sources: [Bambu Lab Security White Paper](https://cdn1.bambulab.com/trust-center/file/bambulab-security-whitepaper-en.pdf),
[Bambu authorization announcement](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/),
[Bambu Developer Mode update](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/),
and [Bambuddy discovery implementation](https://github.com/maziggy/bambuddy/blob/3dcbbdd25e5a86ba267357e7fa4397c5a6569784/backend/app/services/discovery.py).

### Authentication And Sessions

The target must support explicit connection profiles:

| Profile | MQTT identity | Command authorization | File/camera identity | Expected use |
| --- | --- | --- | --- | --- |
| `developer_local` | `bblp` plus LAN access code | Plain request payloads | `bblp` plus LAN access code | LAN-only Developer Mode and older firmware. |
| `authorized_local` | LAN credentials plus a bound client certificate | Signed envelopes for protected commands; some firmware also encrypts G-code payloads | Bound/local credentials as required by each transport | Standard LAN Mode on authorization-enabled firmware. |
| `cloud` | `u_{user_id}` plus account token | Cloud policy and command restrictions apply | Cloud/P2P-specific authorization | Optional cloud-assisted operation. |
| `monitor_only` | Any profile that can subscribe but cannot control | Commands disabled | File/camera capabilities probed independently | Safe fallback when status is available but control is rejected. |
| `mock` | None | Deterministic simulator receipts | In-memory file and camera fixtures | UI and contract testing only. |

Session state must include:

- A monotonically increasing session epoch.
- Connection profile and credential reference, never raw secrets.
- MQTT connection, subscription, and first-valid-message states.
- Last message time and last full snapshot time.
- Printer serial confirmed by telemetry.
- Model, firmware, and capability evidence.
- Whether command authorization has been positively established.
- Per-transport health rather than one global `connected` boolean.

Reconnect behavior must distinguish:

- A broker connection from a usable printer session.
- An expected disconnect from an authentication failure.
- A connected socket with stale telemetry.
- A status-only session from a command-capable session.
- A new session from late messages belonging to an older session.

Authentication failures should fail closed. Transient network failures should
use bounded exponential backoff with jitter. Reconnect must cancel or expire
pending command acknowledgements from the previous session epoch.

### MQTT Topics And Payloads

The commonly observed MQTT interface is:

```text
device/{serial}/request   client to printer
device/{serial}/report    printer to client
```

Cloud and local brokers use the same device topic shape, but they do not
necessarily grant the same command permissions.

Commands are JSON objects grouped by a top-level domain such as `print`,
`pushing`, `info`, `system`, `camera`, or `xcam`. Commands normally include a
string `sequence_id`. Reports may echo:

- top-level domain
- command
- sequence ID
- result
- reason
- error code
- command parameters

The implementation must not assume every command produces a report. It must
also not treat MQTT QoS acknowledgement as printer acceptance.

#### Command Lifecycle

Every outgoing command should produce a receipt with this state machine:

```text
created
  -> published
  -> broker_acked
  -> printer_accepted | printer_rejected | observation_confirmed | timed_out
```

Required receipt fields:

```text
command_id
session_epoch
sequence_id
domain
command
created_at
published_at
deadline
delivery_state
printer_result
printer_reason
printer_error_code
correlation_confidence
```

Correlation should use session epoch, domain, command, and sequence ID.
Observation-based confirmation is valid only for commands with a reliable state
effect, such as a target temperature changing to the requested value.

All command publishes should default to QoS 1. Any exception must be a named
capability/workaround with evidence. A silent QoS 0 fallback changes delivery
semantics and must not happen inside a generic publish helper.

#### State Updates

X1-family telemetry has historically contained broad snapshots. P1 and related
resource-constrained firmware frequently publish sparse deltas. Newer models
add nested fields and model-specific representations.

The adapter therefore emits typed patches, not Moonraker objects:

```text
RawMessage
  -> validated Bambu event
  -> normalized state patch
  -> immutable normalized snapshot
  -> Moonraker projection
```

Patch processing rules:

- Preserve the last known value when a field is absent.
- Distinguish absent, explicit `null`, invalid, and reset-to-default.
- Reject a malformed field without discarding valid siblings.
- Record parse diagnostics without logging credentials or complete sensitive
  captures.
- Apply a full snapshot atomically.
- Timestamp each field group so stale values can be exposed as stale.
- Do not request `pushall` aggressively on resource-constrained models.
- Preserve raw state and raw job identifiers for diagnostics.
- Never infer model support solely because a field happened to be absent once.

Source: [OpenBambuAPI MQTT documentation](https://github.com/Doridian/OpenBambuAPI/blob/5fc53ba61c7eebbe5f78ebdf83dac840f2761cf5/mqtt.md).

### File Transfer And Print Submission

Local file transfer is generally implicit FTPS on TCP `990` with username
`bblp` and the LAN access code. This simple description hides material
generation differences:

| Behavior | Known variation | Required design |
| --- | --- | --- |
| Data-channel TLS | X1/P1-class servers commonly require protected data channels and TLS session reuse. A1/A1 Mini have community-observed transfer hangs and may require clear data-channel fallback while keeping the control channel encrypted. | A model/firmware transport profile chooses TLS behavior. Generic code must not guess after every operation. |
| TLS versions | P2S, X2D, and H2C reports indicate TLS 1.3/session behavior can require pinning the FTP profile to TLS 1.2. | Make TLS bounds profile data with evidence and expiry notes. |
| Upload completion | Some firmware hangs around standard `storbinary()` completion. Manual chunking, progress deadlines, final response handling, and remote size verification are used by maintained projects. | Return an upload receipt and verify the remote artifact before print submission. |
| Paths | Print URL schemes and SD-card roots differ by generation and firmware. Legacy examples include `file:///mnt/sdcard/...`; other flows use `ftp://...` or `ftp:///...`. | A print-submission profile owns path construction. Never concatenate a URL in the Moonraker handler. |
| File visibility | A successful upload does not prove the printer has indexed or can print the file. | Model upload, remote verification, index visibility, and print dispatch as separate stages. |

Print submission is a transaction:

```text
validate local artifact
  -> choose printer path and print profile
  -> upload
  -> verify remote file
  -> build project_file command
  -> await command result or observed job transition
  -> expose Moonraker print_started=true
```

The `project_file` payload varies by generation. Potential fields include file
and URL, plate path, bed type, calibration flags, AMS mappings, dual-nozzle
mapping, and model-specific boolean/integer encodings. These fields belong in a
versioned `PrintSubmissionProfile`, not a universal dictionary.

Sources: [OpenBambuAPI FTP](https://github.com/Doridian/OpenBambuAPI/blob/5fc53ba61c7eebbe5f78ebdf83dac840f2761cf5/ftp.md),
[OpenBambuAPI project_file](https://github.com/Doridian/OpenBambuAPI/blob/5fc53ba61c7eebbe5f78ebdf83dac840f2761cf5/mqtt.md#printproject_file),
and [Bambuddy FTP profiles](https://github.com/maziggy/bambuddy/blob/3dcbbdd25e5a86ba267357e7fa4397c5a6569784/backend/app/services/ftp_profiles.py).

### Status And Event Semantics

Bambu status is not Klipper status. The normalized model must retain Bambu
semantics before projecting them.

Recommended normalized job states:

```text
unknown
idle
preparing
printing
pausing
paused
resuming
cancelling
cancelled
completed
failed
```

The normalized job also retains:

- raw `gcode_state`
- current stage and stage history
- task, subtask, and project identifiers
- filename and plate
- progress and remaining-time source
- current and total layer
- active errors
- pause reason
- timestamps for first seen, started, paused, resumed, and terminal state

Transition policy must be explicit. For example, a brief intermediate state
must not create a new job, and process restart during a print must attach to the
observed job rather than emit a false print-start event.

### Camera Access

Camera access is a separate capability and transport:

| Family | Local transport | Notes |
| --- | --- | --- |
| A1 and P1 | TLS TCP stream on `6000` containing framed JPEG images | Authenticate with `bblp` and access code. Frames can be split across arbitrary reads. |
| X1 and P2S | RTSPS on `322`, typically `/streaming/live/1` | Authenticate with local credentials. |
| H2 family | RTSPS on `322` when LAN live view is enabled | Current firmware may leave the local stream disabled by default. |
| Cloud-assisted | Proprietary P2P, with relay fallback | Bambu states that direct P2P is preferred and server forwarding is used when direct connectivity fails. |

Camera health must not control MQTT health. The Moonraker webcam adapter should
advertise only transports the shim can actually serve to the browser or proxy
reliably.

Sources: [OpenBambuAPI video documentation](https://github.com/Doridian/OpenBambuAPI/blob/5fc53ba61c7eebbe5f78ebdf83dac840f2761cf5/video.md)
and [Bambu Developer Mode update](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/).

### LAN-Only And Cloud-Assisted Behavior

| Mode | Printer internet connection | Local status | Local control | Cloud/app features |
| --- | --- | --- | --- | --- |
| LAN Only, Developer Mode | Disabled | Available through local MQTT | Available through legacy local interfaces, subject to firmware/model behavior | Unavailable |
| LAN Only, Standard Mode | Disabled | Available after local binding | Requires current local authorization path | Unavailable |
| Cloud connected | Enabled | Firmware-dependent; local status may remain available | Local command access can be restricted outside Developer Mode | Available |
| Fully offline removable media | Disabled and no LAN | Not available to shim | Not available to shim | Unavailable |

Do not reduce this to `BAMBU_MODE=local|cloud`. Connection profile, printer
network mode, Developer Mode, and observed command authorization are separate
facts.

## Generation And Firmware Matrix

This matrix is a research baseline, not a support promise.

| Family | Telemetry pattern | Camera | Chamber temperature | Important drift |
| --- | --- | --- | --- | --- |
| A1 / A1 Mini | Sparse/delta behavior should be assumed | JPEG/TLS `6000` | No useful chamber sensor | Community-observed FTP data-channel workaround; newer authorization behavior begins around A1 firmware `01.05.00.00` in one maintained integration. |
| P1P / P1S | Sparse deltas; avoid frequent full refreshes | JPEG/TLS `6000` | No useful chamber sensor | Resource limits, hybrid/cloud control restrictions, and authorization behavior vary by firmware. One maintained integration uses `01.08.02.00` as an encryption-era threshold. |
| X1 / X1C | Broad/full status historically observed | RTSPS `322` | Available | Legacy SD paths differ from newer print URL conventions. One maintained integration uses X1 `01.08.50.32` as an authorization-era threshold. |
| X1E | X1-like, but independently versioned | RTSPS `322` | Available | Do not inherit X1 firmware thresholds without captures. |
| P2S / internal code N7 | Newer nested fields | RTSPS `322` | Available | Secondary auxiliary fan, AMS ID ambiguity, newer authorization, and FTP TLS profile differences. |
| H2D / H2D Pro | Newer nested fields and dual-nozzle state | RTSPS `322`, setting-gated | Available, active heating on supported variants | Calibration value types, nozzle selection, AMS/extruder mapping, and print payload differ from single-nozzle printers. |
| H2S | Newer nested fields | RTSPS `322`, setting-gated | Available | Do not assume dual-nozzle commands from H2D. |
| H2C / internal O1C variants | Tool/nozzle-rack behavior | RTSPS `322`, setting-gated | Available | Tool changer, model-specific file transport profile, and additional stages. |
| A2L, X2D, and future models | Insufficient first-party validation in this project | Capability probe required | Capability probe required | Unknown models use conservative defaults and expose no unsupported Moonraker objects. |

Firmware thresholds above are `community` evidence from
[ha-bambulab capabilities](https://github.com/greghesp/ha-bambulab/blob/34028612155456061e4059a1754527a9774d7db8/custom_components/bambu_lab/pybambu/models.py).
They are useful test hypotheses, not protocol guarantees.

The capability resolver should combine:

```text
declared model
+ normalized model alias
+ firmware modules and versions
+ discovery security flags
+ observed telemetry fields
+ successful non-destructive probes
+ user overrides
= capability with provenance and confidence
```

## Lessons From Contemporary Projects

Projects were reviewed at the commits below. The goal was to compare behavior
and failure handling, not transplant source.

| Project | Reviewed commit | Architectural lesson |
| --- | --- | --- |
| [ha-bambulab](https://github.com/greghesp/ha-bambulab) | `34028612155456061e4059a1754527a9774d7db8` | Maintains a normalized device model, explicit feature checks by model and firmware, watchdog recovery, reconnect backoff, certificate bundles, and a broad model fixture corpus. |
| [Bambuddy](https://github.com/maziggy/bambuddy) | `3dcbbdd25e5a86ba267357e7fa4397c5a6569784` | Separates MQTT, FTP, discovery, camera, and virtual-printer transports; handles stale-but-connected MQTT sessions; records model-specific FTP/camera profiles; tests multi-message lifecycle and firmware quirks. |
| [OctoPrint-BambuPrinter](https://github.com/jneilliii/OctoPrint-BambuPrinter) | `c1e96b230618d55862cb34cfa5661b8d12f1ad96` | Makes external API lifecycle translation explicit with idle, printing, and paused states rather than scattering transitions across routes. |
| [bambulabs_api](https://github.com/BambuTools/bambulabs_api) | `5bd1e84a9b0c21ab7cdfdccac9dab43994319b0d` | Keeps MQTT, FTP, and camera clients separate and demonstrates FTPS session-reuse and framed-camera concerns. |
| [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) | `5fc53ba61c7eebbe5f78ebdf83dac840f2761cf5` | Provides a protocol notebook for local/cloud MQTT, FTPS, TLS validation, camera variants, command reports, and current authorization captures. |

### Behavioral Compatibility Matrix

| Concern | Robust pattern | Required behavior in this shim |
| --- | --- | --- |
| Reconnects | Backoff plus watchdog for stale sessions | Reconnect per transport, invalidate old command receipts, and require fresh telemetry before declaring ready. |
| Partial updates | Merge validated deltas into a retained snapshot | Patch normalized state; never rebuild Moonraker state from one MQTT message. |
| Malformed updates | Parse defensively at field boundaries | Keep valid siblings, retain last known values, and emit diagnostics. |
| Command acknowledgement | Correlate reports and observe state effects | Return honest pending/accepted/rejected/timeout results instead of immediate success after publish. |
| Capability detection | Model plus firmware plus observed state | Expose capability provenance and conservative unknown-model defaults. |
| Firmware drift | Profiles, fixtures, and versioned rules | No inline string tests spread across route handlers. |
| Local credentials | Distinct access-code and certificate flows | Secret references, redacted logs, explicit session profile, and no secrets in captures. |
| Transport isolation | Separate MQTT, FTP, camera, and discovery services | One transport failure cannot mark every service down. |
| Fixtures | Model-specific full snapshots and ordered delta sequences | Build a sanitized fixture corpus keyed by model, firmware, and mode. |
| Compatibility translation | Domain model before external API projection | Moonraker handlers consume normalized state and intents only. |

## Target Architecture

```text
Mainsail / Moonraker clients
             |
    Moonraker compatibility API
             |
      Moonraker projection
             |
   normalized printer domain
             |
      Bambu protocol adapter
       /      |      |      \
 discovery  MQTT   files   camera
             |
 authentication/session profiles
```

### Package Boundaries

```text
bambu_moonraker_shim/
  domain/
    capabilities.py
    commands.py
    events.py
    files.py
    jobs.py
    printer.py
    state_store.py
  bambu/
    adapter.py
    auth/
    discovery/
    mqtt/
    files/
    camera/
    profiles/
  moonraker/
    api.py
    objects.py
    files.py
    commands.py
    websocket.py
  simulator/
    printer.py
    files.py
    scenarios.py
  diagnostics/
    capture.py
    redaction.py
```

Dependency direction is enforced:

```text
moonraker -> domain <- bambu
simulator -> domain
```

The domain package imports neither MQTT nor Moonraker HTTP types.

### Normalized Domain

The normalized snapshot should include:

- `identity`: serial, display name, declared model, normalized family.
- `session`: profile, epoch, per-transport health, last-seen timestamps.
- `capabilities`: value, evidence source, confidence, and optional firmware
  constraint for every feature.
- `job`: normalized and raw lifecycle state.
- `motion`: observed position and homing confidence.
- `thermal`: available sensors, heaters, current values, targets, and target
  source.
- `fans`: named fans with actual availability and normalized speed.
- `materials`: external spool, AMS units, trays, routing, and active tool.
- `lights`: independently available light nodes.
- `files`: remote roots, cache/index state, and storage information.
- `camera`: transport, availability, endpoint, and health.
- `errors`: active Bambu HMS errors with raw codes and normalized severity.
- `raw`: bounded diagnostic metadata, not an unbounded copy of all traffic.

State is immutable to readers. The store applies one patch under one revision,
then publishes a change event containing the old revision, new revision, and
changed paths.

### Adapter Contracts

The Bambu adapter exposes intents, not MQTT payload dictionaries:

```text
pause_job()
resume_job()
cancel_job()
submit_print(PrintSubmission)
set_temperature(HeaterId, value)
set_fan(FanId, ratio)
set_light(LightId, enabled)
move(MoveIntent)
load_material(MaterialRoute)
unload_material(ToolId)
skip_objects(ObjectIds)
```

Each call returns a command receipt. Unsupported commands return
`unsupported` before reaching a transport.

Profiles translate intents into wire payloads:

- `MqttCommandProfile`
- `PrintSubmissionProfile`
- `FileTransportProfile`
- `CameraProfile`
- `CapabilityProfile`

Profile selection is observable in diagnostics and testable without opening a
network connection.

## Moonraker Boundary

Moonraker emulation should advertise only what can be supported honestly.
Mainsail discovers behavior from server metadata, loaded printer objects, file
roots, macros, and webcam records. Returning invented objects is not harmless;
it causes the UI to offer controls that cannot work.

### Compatibility Levels

| Level | Meaning |
| --- | --- |
| `native` | Bambu has a direct operation or state with equivalent semantics. |
| `projected` | The value is a documented projection with bounded semantic differences. |
| `simulated` | The value exists only to satisfy a client contract and is labeled accordingly. |
| `unsupported` | The endpoint or object is omitted or returns a clear unsupported error. |

### Moonraker Honesty Matrix

| Moonraker surface | Level | Policy |
| --- | --- | --- |
| `server.info`, connection identify, websocket ID | `projected` | Report shim identity and components, never claim a real Klippy process. |
| `printer.info` | `projected` | Report readiness from normalized session state. |
| `printer.objects.list/query/subscribe` | `projected` | Include only objects enabled by current capabilities. Send sparse changed fields after an initial snapshot. |
| `extruder`, `heater_bed`, real chamber heater/sensor | `projected` | Expose only observed sensors. Locally requested targets may be marked pending until telemetry confirms them. |
| `fan` and named generic fans | `projected` | Advertise only physically supported fans. Preserve unavailable versus zero speed. |
| `print_stats` | `projected` | Map lifecycle explicitly and retain terminal state until the next job/reset policy. Mark inferred durations internally. |
| `virtual_sdcard` | `projected` | Represent the active Bambu file and progress; do not pretend byte position is measured when it is inferred. |
| `display_status` | `projected` | Use normalized job progress and stage text. |
| `pause_resume` and standard print controls | `native` or `projected` | Complete only when command acceptance or reliable state observation occurs. |
| `server.files.*` on `gcodes` | `projected` | Back with printer storage and a coherent cache. Upload-then-print is transactional. |
| `config` root | `simulated` | Serve the minimal read-only compatibility files required by clients. Do not imply printer configuration can be edited. |
| `exclude_object` | `projected` | Advertise only when object IDs are known from the active 3MF/job. Maintain Bambu ID to Moonraker name mapping. |
| G-code macros | `projected` | Advertise only implemented, capability-gated macros. Parse to typed intents. |
| Arbitrary G-code | `projected` or `unsupported` | Developer sessions may allow a restricted path. Authorized firmware can require encrypted/signed G-code. |
| Position and homed axes | `projected` | Do not claim homed `xyz` unless telemetry establishes it. |
| Webcam APIs | `projected` | Advertise a shim-proxied URL only when the corresponding camera adapter is healthy. |
| Firmware restart, Klipper restart, machine power/reboot | `unsupported` | These are not equivalent to Bambu operations. |
| Emergency stop | `unsupported` | Cancel is not an emergency stop and must not be relabeled. |
| Klipper config mutation, update manager, package management | `unsupported` | Omit components and routes. |

Moonraker references:
[external API introduction](https://moonraker.readthedocs.io/en/latest/external_api/introduction/),
[printer objects](https://moonraker.readthedocs.io/en/latest/printer_objects/),
and [file management](https://moonraker.readthedocs.io/en/latest/external_api/file_manager/).

## Current Implementation Audit

The current code remains useful as a behavioral notebook, but it is not the
target architecture.

| Current area | Load-bearing concern |
| --- | --- |
| [`moonraker_api.py`](../bambu_moonraker_shim/moonraker_api.py) | HTTP routes, JSON-RPC, Mainsail compatibility policy, macro parsing, mock behavior, file orchestration, state mutation, and transport calls are fused in one large module. |
| [`bambu_client.py`](../bambu_moonraker_shim/bambu_client.py) | MQTT lifecycle, telemetry translation, model switches, mock simulation, command construction, and local target reconciliation share one class. |
| [`state_manager.py`](../bambu_moonraker_shim/state_manager.py) | Stores Moonraker-shaped state directly, so Bambu facts and translation decisions cannot be tested independently. |
| [`ftps_client.py`](../bambu_moonraker_shim/ftps_client.py) | Contains valuable transport discoveries, but model profiles and transactional print submission are not domain contracts. |
| [`camera_manager.py`](../bambu_moonraker_shim/camera_manager.py) | A useful transport boundary that should be retained conceptually, then generalized through camera capabilities/profiles. |
| Mock mode | Exercises Mainsail pathways, but shares production classes and does not replay model/firmware-specific protocol scenarios. |
| Tests | Cover selected commands and helpers but have no sanitized model telemetry corpus or ordered reconnect/job lifecycle captures. |

Useful discoveries should be converted into fixtures and contract tests before
their old implementation is removed.

## Test And Capture Strategy

### Capture Manifest

Every sanitized capture set should include:

```yaml
schema_version: 1
printer_family: P1S
printer_model_raw: BL-P001S
firmware:
  ota: 01.00.00.00
connection_profile: developer_local
network_mode: lan_only
developer_mode: true
captured_at: 2026-01-01T00:00:00Z
evidence: observed
redactions:
  - serial
  - access_code
  - account_id
  - tokens
  - certificate_private_key
```

Capture payloads must replace identifiers consistently so correlation remains
testable. Private keys, tokens, access codes, Wi-Fi credentials, public IPs,
and cloud URLs with signatures must never enter the repository.

### Fixture Types

- Discovery announcements and M-SEARCH responses.
- Successful and failed authentication handshakes as metadata, not secrets.
- Initial full snapshots.
- Ordered sparse delta streams.
- Malformed-field and unknown-field cases.
- Command success, rejection, silence, and late acknowledgement.
- Reconnect with late messages from an old epoch.
- Print lifecycle from submission through each terminal state.
- Upload interruption, retry, remote verification, and stale file index.
- Camera framing across arbitrary TCP chunk boundaries.
- Per-model capability and firmware-boundary cases.

### Test Layers

1. Protocol parser tests operate on raw sanitized messages.
2. Profile tests map typed intents to expected wire messages.
3. State reducer tests replay ordered events into normalized snapshots.
4. Capability tests explain every decision and evidence source.
5. Moonraker projection tests use normalized snapshots only.
6. Simulator contract tests run Mainsail-relevant workflows with no printer.
7. Hardware contract tests are opt-in and record model/firmware metadata.
8. Cross-project regression cases are rewritten as independent behavioral
   fixtures, never copied tests.

## Migration Backlog

### Phase 0: Evidence And Safety Rails

- Adopt this document as an architecture decision record.
- Add capture schema, redaction tooling, and fixture licensing rules.
- Record current command and telemetry discoveries as tests before refactoring.
- Establish support labels: verified, experimental, monitor-only, unsupported.

### Phase 1: Normalized Domain

- Implement immutable normalized snapshots and typed patches.
- Implement job lifecycle reducer and per-field freshness.
- Implement capability values with provenance.
- Move mock behavior into a deterministic domain simulator.

### Phase 2: Transport Adapters

- Split discovery, authentication, MQTT, FTPS, and camera packages.
- Add session epochs, stale-session watchdog, and bounded reconnect backoff.
- Add command receipts and response correlation.
- Introduce versioned model/firmware transport profiles.

### Phase 3: Print Transaction

- Separate upload, verification, index visibility, command dispatch, and job
  observation.
- Add model-specific print submission profiles.
- Return `print_started` only after accepted or observed start.
- Reconcile remote storage cache after partial failures.

### Phase 4: Moonraker Projection

- Rebuild object list/query/subscribe from normalized state.
- Implement the honesty matrix and capability-gated macros.
- Keep Mainsail compatibility fixtures at the API boundary.
- Clearly reject unsupported Klipper and machine operations.

### Phase 5: Model Validation

- Validate at least one owned device per supported family.
- Record firmware-boundary captures for authorization and FTP profiles.
- Add unknown-model conservative behavior.
- Publish the tested model/firmware matrix from fixture metadata.

## Exit Criteria

The overhaul is ready to replace the legacy path when:

- Moonraker code has no imports from MQTT, FTP, camera, or discovery packages.
- Bambu transports have no imports from HTTP, JSON-RPC, or Moonraker objects.
- A sparse telemetry replay produces the same normalized state as an equivalent
  full snapshot.
- Every command returns an honest receipt and expires across session epochs.
- File upload and print submission are a tested transaction.
- Unsupported Moonraker operations are not advertised.
- Mainsail file browsing, monitoring, print submission, pause/resume/cancel,
  capability-gated controls, and mock workflows pass contract tests.
- The supported model list is generated from validated fixture metadata, not a
  README claim.

## Open Research Questions

- What is the supported, non-extracted enrollment flow for Standard LAN client
  certificates across current retail models?
- Which exact commands require signed envelopes on each firmware line?
- Which models require encrypted `gcode_line` parameters, and how is the
  printer public key obtained through a supported flow?
- Which print URL and remote-root combinations are accepted by each model and
  firmware?
- Which MQTT reports are reliable command acknowledgements versus echoes?
- How should active cloud connection and local control coexist per generation?
- Which camera streams can be redistributed safely and reliably to browser
  clients?
- What state transition uniquely distinguishes completed, cancelled, and failed
  jobs on each family?
- Which storage fields are authoritative enough to implement Moonraker disk
  usage?
- What minimum Mainsail API contract can be versioned and tested independently
  of Mainsail release churn?
