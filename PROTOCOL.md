# VRS DirectForce wheel-base USB configuration protocol — SPEC

Status as of 2026-11-08. Reverse-engineered from VRS's own `direct_force_sdk`
(shipped inside `DirectForce.1.0.0.1.app.exe`, the Windows configuration app),
verified byte-for-byte against recorded traffic of the official Windows tool on
the author's own base, and validated live against a DFP15
(fw 1.15, hw 4.0). Status of every claim is marked: **[SDK-proven]** (decoded
from the SDK binary or struct-probed through the SDK itself), **[inferred]**
(consistent reading of the disassembly), **[TBD]** (must be confirmed against a live base).

Implementations: `vrs-config.py` (CLI) and `vrs-config-gui.py` (Tk desktop GUI)
in this repository.

---

## 1. Scope and hardware model

- Applicable devices **[SDK-proven]**:
  - Wheel bases: VID `0x0483`, PIDs `0xA355` (DFP15 / DirectForce Pro family —
    `kDirectForceWheelBasePid`), `0xCB52` (second base PID seen in SDK strings).
  - Other VRS USB PIDs exist (pedals `kDirectForcePedalsPid`, button plates
    `kDirectForceButtonPlatePid`, BLE variants) and use the same feature-report
    transport family, but this spec documents the wheel base.
- The base is an STM32-style USB HID device. Linux binds it with
  `hid-universal-pidff` for force feedback; that driver is unrelated to the
  configuration protocol described here — config traffic is vendor HID
  reports, not FFB effect uploads.
- Device collections in the report descriptor **[SDK-proven]** (read from
  `/dev/hidraw8` via `HIDIOCGRDESC`, 949 bytes):
  - standard joystick/haptic collection (input report `0x01`, output effect
    reports `0x01`–`0x14` — used by pidff, ignored here);
  - vendor collection `Usage Page 0xFF00, Usage 1`:
    - output report `0x64`, 63 bytes
    - input report  `0x65`, 63 bytes
    - feature report `0x66`, up to 511 data bytes (512-byte buffers with the
      report id byte).
  - The SDK additionally documents "64-byte" feature exchanges; the base
    accepts feature buffers of both sizes (`kFeatureReport64Size = 64`,
    `kFeatureReportSize = 512`).

## 2. Transport

> LINUX STATUS (2026-11-08, RESOLVED): this exact transport now WORKS on
> Linux via `vrs-config.py --raw` (raw usbdevfs, iface-recipient, unbound
> driver) - live-validated end-to-end. Key firmware rule: every connection
> = ch00 handshake first, ch01 commit last (see §11). Bare GET_REPORTs
> without that gating, and device-recipient class requests, are STALLed by
> design - they are NOT a wedge.


Every protocol exchange is a feature-report round trip on report id `0x66`:

```
SET_FEATURE(0x66, request frame)   ->   GET_FEATURE(0x66, 512) reply
```

Recorded tool traffic shows this is literally raw EP0 `SET_REPORT`/`GET_REPORT`
(class output, `bmRequestType 0x21 bRequest 0x09 wValue 0x0366 wIndex 0
wLength 512` — data INCLUDES the leading report-id byte — then `0xA1 0x01`
GET, same sizes). Linux `HIDIOCSFEATURE/HIDIOCGFEATURE` produce exactly these
transfers, so `vrs-config.py` uses the identical transport. The SDK sleeps
≈50 ms between SET and GET. Vendor interrupt traffic (`0x64`/`0x65`) is NOT
used for configuration — the only interrupt traffic on the wire is the
1 kHz joystick input stream.

Sequencing (VRS One behavior, reproduced by the tool):
`client_id` = random u32 fixed for the connection; `sequence_id` = fresh
random u32 per logical OPERATION (constant within one read/save burst);
`request_id` = u32 counter incrementing 0,1,2… per frame for the connection.
Replies echo channel/client_id/sequence/request/offset verbatim.

## 3. Frame layout

Little-endian. Identical header for requests and replies:

| offset | size | field | notes |
|---|---|---|---|
| 0 | 1 | report id | always `0x66` |
| 1 | 1 | **channel** | protocol selector, §5 |
| 2 | 4 | client_id | random u32, constant per connection |
| 6 | 2 | payload_size | 0 for pure reads; reply: bytes at [32:] |
| 8 | 2 | offset | chunked transfers (§7) |
| 10 | 4 | sequence_id | random u32 per logical operation |
| 14 | 4 | request_id | counter 0,1,2… per connection |
| 18 | 1 | calculate_crc flag | always `1` |
| 19 | 13 | var-header | **13 zero bytes on every request and reply** |
| 32 | N | **protobuf payload** | §6 |
| … | | zero padding | up to the CRC |
| 508 | 4 | CRC32 | §4 — **confirmed slot** |

All 54 recorded requests and all 45 replies match this layout exactly,
including the 13-byte zero var-header in `[19:32]` — the payload always
starts at byte **32**, never 19 (this corrects the earlier struct-probing
interpretation: `payload_set()` writing at 8/19 in the C++ object is a
storage alias, the wire position is 32).

Short variant `VrsFeatureReportShortHeader` **[SDK-proven]**: byte 0
report_id, byte 1 subreport_id (= channel here), byte 2 client_id (u8),
byte 3 calculate_crc. `VrsFeatureReportsVarHeader` (512-byte chunked form)
**[SDK-proven]**: report_id u8 @0, subreport_id u8 @1, client_id u16 @2,
payload_size u16 @4, block_num u8 @6.

### 64-byte form (legacy / `kFeatureReport64Size`)
Same header, CRC over bytes 0–59 stored at 60–63 **[SDK-proven]**; not used
in practice (VRS One only sends 512-byte frames).

## 4. CRC

Reflected CRC-32 (poly `0xEDB88320`, init `0xFFFFFFFF`, final XOR) — the
standard `zlib.crc32`. Two CRC tables (normal + bit-reversed word order)
exist in the SDK DLL at file offsets `0x166bc0` / `0x166fc0`; there is **no**
CRC-16.

512-byte frame: CRC32 over bytes `[0:508]` stored little-endian at `508`.
**This slot is settled** — it validated on every single one of the 45 device
replies and 54 host requests in the recorded traffic (`selftest: CRC ALL OK`).
The old 508-vs-509 auto-probe is gone; `parse_frame` checks `[0:508]@508`.

64-byte frame: CRC over `[0:60]` stored at `60` **[SDK-proven]**.

## 5. Channels (frame byte 1)

Semantics below are from real VRS One traffic on a healthy DFP15 (45 transaction
pairs + full save bursts recorded by the author), cross-checked against the
SDK channel scan (former §5 inference, now superseded where they differ):

| channel | direction | meaning **[proof]** |
|---|---|---|
| `0x00` | read | handshake echo — sz 0 request, sz 0 reply (first frame of every burst). **Benign, NOT the old bootloader suspicion.** |
| `0x02` | read | settings header → protobuf `SettingsHeader`: `{num_profiles, current_profile, max_num_profiles, field5(=3)}` (sample: 2/1/100/3) |
| `0x04` | read | profile by index. Request payload = protobuf `{1: idx}` with idx starting at 1 (e.g. `08 01`); sz 0 request returns the CURRENT profile. Reply = full profile blob (protobuf `SimWheelBase.SettingsProfile`, §6) |
| `0x07` | read | **device versions** → `DeviceVersions` (sample: hw 4.0, fw 1.15, bootloader 1.15, build hash + `Nov 27 2025`) |
| `0x0E` | read | **schema download** — chunked, `offset` steps 0/476/952/1428, chunk size 476 (last 176), total 1604B gzip payload starting at [32]; gunzips to the device's own `SimWheelBase.proto` source text |
| `0x15` | read | small status reply, sz 4 `08 01 18 0a` (`{1:1, 3:10}`) |
| `0x17` | read | profile header list, sz 16 (`{1:{2:'Default'},…}`) |
| `0x01` | write | commit — sz 0, final frame of a save burst |
| `0x03` | write | settings header (payload = same 8B `SettingsHeader` as 0x02 reply) |
| `0x06` | write | profile blob (payload = exact bytes read back via 0x04) |
| `0x16` | write | header variant, payload `{1:1, 3:10}` (`08 01 18 0a`) |
| `0x19`/`0x18` | write | profile header 16B, payload `{1:{2:name},3:float}` — both variants seen (0x19 ×2, 0x18 ×1) |
| `0x05` | write | profile blob variant of 0x06 (seen once, in the same role) |
| `0x08`,`0x09` | — | **FORBIDDEN** — fw/bootloader region (SDK channel map; never exercised on the wire here) |

**Save burst sequence (observed twice, identical):** `0x00` → `0x16` → `0x19`
→ `0x03` → `0x06`×N (one per profile) → `0x01` (commit). Every write reply is
an sz 0 ACK that echoes the request counters.

`raw`/`sweep` gate everything in the write list behind `--allow-write`;
`0x08`/`0x09` are refused outright (only `--force` bypasses, and there is no
legitimate reason to use it against the rig).

## 6. Payloads

- **Profiles travel as PROTOBUF** (this supersedes the earlier "raw struct"
  inference): `0x04` replies are serialized `SimWheelBase.SettingsProfile`
  messages, and the same bytes are written back verbatim via `0x06` in save
  bursts. The device **ships its own schema**: channel `0x0E` returns a
  1604-byte gzip of `SimWheelBase.proto` source text — saved in this repo at
  `SimWheelBase.proto` and retrievable anytime with `vrs-config --raw schema`.
  Key `SettingsProfile` fields: `1 profile_header
  (SimSettings.SettingsProfileHeader{1 number,2 name})`, `3 rotationDegrees`
  (180..2160), `6 ffbForce` (0..100), `7 smoothing` (0..10), `8 damping`,
  `9 friction`, `10 inertia`, `11 spring` (0..100), `12..15` effect overrides
  (enum), `16 smoothingType`, `17 staticForceReduction`, `18 nonLinearity`,
  `19 slewRate` (enum), `20 endStopMaxForce`, `21 endStopStiffness`,
  `22 softEndStopDegrees`, `23 isSoftEndStopEnabled`, `24 torqueUpperBound`,
  `30 filterMode`, `31 motorSlewRate`, `32 softwareSlewRate`,
  `33 predictionLevel`, `34 predictionPeakDamp`, `35 stepBandStartFreq`,
  `36 stepBandDamping`, `37 lowPassCutoffFreq`; reserved 2/4/5.
  Sample from the device: profile "Default" (85B) = rotation 900,
  ffbForce 80, smoothing 7, damping 10, endStopMaxForce 100, torqueUpperBound
  15, stepBand 1000/0/1000; profile "NextGen-AC" = 90B.
- The full field-number→name map used by the tool's decoder is in
  `vrs-config.py` (`PROFILE_FIELDS`), generated from that schema.
- Command ids, setting ids, option ids remain **device-queried tables** in the
  SDK (`GetSupportedCommands/Settings/Options`); recorded traffic shows VRS One
  does NOT need them for the standard read/save cycle — the channels above
  carry everything.
- `SimSettings.proto` (extracted from the SDK DLL) covers the
  non-profile messages, confirmed against recorded replies:

```proto
message SettingsHeader { uint32 num_profiles=1; uint32 current_profile=2;
  uint32 max_num_profiles=3; uint32 settings_version=4; uint32 config_byte_a=5; }
message SettingsProfileHeader { uint32 profile_number=1; string profile_name=2; }
message Version { uint32 major=1; uint32 minor=2; }
message FwBuildInfo { int32 hash=1; string build_date=2; }
message DeviceVersions { Version hardware_rev=1; Version firmware_rev=2;
  Version bootloader_rev=3; FwBuildInfo build_info=4; }
message Calibration { int32 step_result=1; string tip_message=2; }
```

  (the DLL-side `SettingsProfile{profile_header=1}` wrapper only carries the
  header — the profile BODY is the `SimWheelBase.SettingsProfile` above.)
- `VRS_DFP_WheelBase.json` shipped in the app is **not** the wire protocol: it
  is the rFactor-style game data map (86 named FFB settings with ranges such
  as `Brake effects strength` −10000..10000). Useful later for the CLI's
  human-friendly names; keep it as a reference table only.

## 7. Chunked transfers

Large payloads stream on the SAME `0x66` feature report: continuation is
requested by repeating the channel (`0x0E` schema download observed) with
`offset += last chunk size` in the request header — offsets 0/476/952/1428,
chunk sizes 476/476/476/176, total 1604 bytes — until a reply arrives with
`payload_size < 476`. Data is at `[32:32+sz]` in each reply. No special block
report ids appear on the wire; the SDK constants `kFeatureReportsVarId*`
(0x67-0x6F) belong to its internal buffering. `vrs-config --raw schema`
implements this loop and gunzips the result.

## 8. Constants reference **[SDK-proven — read live from the SDK]**

| name | value |
|---|---|
| `kDirectForceVid` | `0x0483` (1155) |
| `kDirectForceWheelBasePid` | `0xA355` (41813) |
| `kFeatureReportSize` | 512 |
| `kFeatureReport64Size` | 64 |
| `kPayload64Size` | 52 |
| `kCrc32Size` | 4 |
| `kFeatureReportsMaxSize` | 512 |
| `kFeatureReportsVarBlockSize` | 64 |
| `kFeatureReportsVarHeaderSize` | 12 |
| `kFeatureReportsVarIdOffset/First/Last` | 103 / 104 / 111 (0x67/0x68/0x6F) |
| `kFeatureReportsVarPayloadMaxSize` | 480 |

## 9. Safety matrix

| action | policy |
|---|---|
| read channels §5 (`0x00/0x02/0x04/0x07/0x0E/0x15/0x17`) | allowed by default; **proven safe on the wire** — these are exactly the requests VRS One itself sends |
| `0x01/0x03/0x05/0x06/0x16/0x18/0x19` writes (save-burst channels) | require `--allow-write`, refused by `sweep`; profile writes should only carry bytes previously read back via `0x04` |
| `0x08/0x09` + any fw/bootloader command | **forbidden**; tool refuses without `--force`; SDK-side `EnterBootloader*/ExitBootloader/StartFwUpdate/FactoryReset` are never exercised by this project |
| firmware state | user's DFP15 is on the latest base firmware (v1.15, verified with the official Windows tool + in the recorded `DeviceVersions`) |


## Appendix A. Running it on Linux

- **Transport that works:** raw USB via `/dev/bus/usb/BBB/DDD` (`usbdevfs`
  `USBDEVFS_CONTROL`), class+**interface**-recipient `SET_REPORT`/`GET_REPORT`
  exactly like Windows hidclass sends them. The kernel HID driver must be
  unbound from the interface first (`/sys/bus/usb/drivers/usbhid/unbind`, root),
  then `USBDEVFS_CLAIMINTERFACE` (`0x8004550F`) - both are done automatically by
  `vrs-config --raw` / the GUI. Replug the USB cable afterwards and the
  joystick + force feedback (`hid-universal-pidff`) come back.
- **hidraw ioctls** (`HIDIOCGFEATURE`/`HIDIOCSFEATURE`) embed the buffer size in
  the request code - `fcntl.ioctl` cannot carry them; use `libc.ioctl` via
  ctypes. Pointer-typed usbdevfs ioctls (`SUBMITURB`/`REAPURBNDELAY`) hit a
  CPython `SystemError` through `fcntl` as well - raw `libc.ioctl` works.
- **Connection gating is mandatory** (see §2): a session that starts without the
  ch-`0x00` handshake (as request #0 of a fresh random `client_id`) or that is
  abandoned without the ch-`0x01` commit leaves the base's connection table
  occupied and the next handshake stalls (`EPIPE` / `EINVAL`). The stall is
  *not* damage: `USBDEVFS_RESET` (`0x00005514`) on the bus node clears it
  unprivileged - `vrs-config --recovery` (CLI) or the GUI's
  **Recover (USB reset)** button.
- **One instance:** the usbdevfs interface claim is exclusive and the base
  supports a single gated connection, so only one `vrs-config`/
  `vrs-config-gui` may run against the base at a time; a second instance
  conflicts with the first and can leave a stale connection - in that case
  use **Recover (USB reset)** (GUI) or `vrs-config --recovery` (CLI)
  before starting again.
- **udev:** `udev/99-vrs-dfp.rules` (run `scripts/install-udev.sh`) gives the
  seat user access to the 0483 USB + hidraw nodes.
- **Wine/Proton:** the official app does render under Wine with enough DLL
  overrides, but no Wine build implements the HID feature-report path, so
  settings I/O is impossible there - and abandoned sessions wedge the base's
  connection table until a USB reset. Use this tool instead.

## Appendix B. Development history (condensed)

1. SDK extracted from the official single-file app; the config API is a SWIG
   wrapper over a statically linked `direct_force_sdk` (hidapi + protobuf).
2. Frame layout decoded by sentinel-diffing the SDK's own report objects;
   channel map from disassembling the wrapper call tree.
3. Ground truth from recorded traffic of the official tool: transport, CRC placement,
   read channels, the full save burst, handshake/commit gating.
4. A second traffic log proved **profile create = plain save burst** with an extra
   blob (no dedicated command; the SDK's `CreateProfile` channel is something
   else entirely and touching it destructively repacks the store - never use
   channels 0x1a/0x14).
5. Live validation on a real DFP15: reads, writes, GUI profile management.

Protocol knowledge in this file is public. **Firmware/bootloader channels
(0x08/0x09) were never exercised and are unknown - hard-refused by the tools.**
