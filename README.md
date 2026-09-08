# Hikvision AX Pro Lite

Basic local alarm control for Home Assistant, with minimal polling of the Hikvision AX Pro panel.

This fork is intentionally focused: **see the alarm state, Arm Home / Stay, Arm Away, and Disarm**. It connects directly to the panel over ISAPI; no cloud connection is required by the integration.

## Confirmed working

The repository owner has confirmed the lite integration working on:

| Panel | Firmware |
| --- | --- |
| **DS-PWA96-M-WB** | **V1.3.1 build 251113** |

This confirms the owner's installation, not every AX Pro model or firmware combination. If you test another combination, please share the model, firmware and results in this repository's [issues](https://github.com/Verpz/hikaxpro_hacs_lite/issues).

## Why this fork exists

The full upstream integration polls zones, peripherals and panel diagnostics as well as alarm status. On the owner's V1.3.1 installation, repeated polling coincided with a slow panel web interface and delayed-feeling arm/disarm operation. Disabling the integration improved responsiveness in an A/B test.

This fork reduces the work requested from the panel. It does not attempt to fix the firmware or establish the cause of every panel or wireless-zone issue.

## What Home Assistant exposes

One main `alarm_control_panel` entity supports:

- **Arm Home / Stay**
- **Arm Away**
- **Disarm**

Reported states include disarmed, armed home, armed away, exit delay (`arming`), and triggered when reported by subsystem status. Armed vacation is also displayed if reported by the panel; a vacation command is not exposed.

Optional subsystem alarm entities display and control individual areas. They share the same status request and do not add separate polling.

Only the alarm control panel platform loads. Detector/contact/motion entities, batteries, signal strength, relays, sirens, keypads, repeaters, AC diagnostics, switches and buttons are outside this fork's scope. There is no automatic zone bypass or custom bypass, siren or one-key-alarm service. The integration's reload service remains available.

## Polling and update delay

| Situation | Behaviour |
| --- | --- |
| Startup | Read MAC address and device information, then one initial subsystem-status refresh, plus authentication as needed |
| Normal background operation | One subsystem-status request every **120 seconds** by default |
| Successful command from HA | Send the command, then one immediate subsystem-status refresh |
| Change from keypad or Hik-Connect | Picked up at the next poll; up to **120 seconds** with the default interval |

Periodic polling only fetches `/ISAPI/SecurityCP/status/subSystems`. It does not sweep zones, peripherals, batteries or AC status.

The default interval is **120 seconds**, and the configurable minimum is **60 seconds**. Existing stored intervals below 60 seconds, including the old 30-second setting, use 120 seconds at runtime. Values of 60 seconds or more are preserved. The options form shows the effective interval, and saving options reloads the integration automatically.

**Push events are not implemented.** Upstream has investigated AX Pro's event stream, but this fork currently uses polling. An immediate refresh after an HA command may show exit delay; completion is picked up at the next poll.

## Installation

### HACS

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/Verpz/hikaxpro_hacs_lite` with category **Integration**.
3. Download this integration from HACS.
4. Restart Home Assistant.
5. For a new installation, add **Hikvision AX Pro Lite** under **Settings → Devices & services**.

### Manual

1. Download the [master branch ZIP](https://github.com/Verpz/hikaxpro_hacs_lite/archive/refs/heads/master.zip) and extract it.
2. Copy the extracted `custom_components/hikvision_axpro` directory into Home Assistant's `config/custom_components/` directory.
3. Restart Home Assistant, then add the integration if it is not already configured.

The installed manifest should be at `config/custom_components/hikvision_axpro/manifest.json`.

### Moving from the upstream integration

This fork keeps the **`hikvision_axpro` integration domain** and existing alarm entity unique IDs. Existing credentials, alarm codes and subsystem settings remain usable; there is no domain migration or need to recreate the config entry solely to switch forks.

- Replace the upstream integration files with this fork. If using HACS, ensure it manages this repository for future updates.
- Restart Home Assistant after replacing the files.
- Update automations that depended on detector/peripheral entities or removed custom services.
- Old detector/peripheral entries may remain unavailable in HA's entity registry. This fork does not automatically delete them.

The upstream integration and this fork share a domain and cannot run side by side as separate integrations.

## Configuration

Supply the panel's local host/IP address and valid panel username/password. Use an account with the permissions required for local alarm control; the integration requests the Admin/Operator user level. These credentials authenticate to the panel, not to Home Assistant Cloud.

The existing options for an alarm code, numeric/text code format, requiring the code when arming, scan interval and optional subsystem panels remain available. Alarm code checks in Home Assistant are retained separately from panel login credentials.

If the panel refuses to arm, check its open zones, faults and account permissions in its own interface or Hik-Connect. This integration does not automatically bypass blocking zones.

## State and connection handling

Invalid, empty or unknown subsystem status marks the alarm **unavailable**, rather than falsely reporting disarmed. A later valid refresh restores availability.

The main entity represents all enabled areas, even when optional subsystem entities are enabled. Its state is independent of the order returned by the panel, using this precedence:

**Triggered → Arming → Armed Away → Armed Vacation → Armed Home → Disarmed**

For mixed area states, the highest-priority state is displayed. Enable subsystem entities to see each area separately. Main-panel commands address all areas; subsystem commands address their own area.

The dependency remains pinned to **`hikaxpro==2.3.0`**. A local client subclass retains its alarm commands and password encoding while limiting authentication recovery to one login attempt and one request retry. A failed login stops the retry path.

Every HTTP call, including login, has a **3-second connect timeout and 5-second read timeout**. These are socket timeouts, not a hard deadline for a complete operation. Calls on each client are serialized, and the integration awaits the worker rather than abandoning it after an asynchronous timeout.

## Development and validation

Run the tests with:

```sh
python -m pip install -r test_requirements.txt
python -m pytest
python -m compileall -q custom_components
```

The current suite contains **88 passing tests**, run locally with Python 3.12 and Home Assistant 2025.1.4. A GitHub Actions workflow runs the unit tests alongside the repository's HACS/Hassfest validation workflows.

Regression coverage includes subsystem-only polling, startup request counts, one refresh after commands, code protection, legacy scan intervals, invalid responses, exit delay, multi-area ordering, options reload and bounded authentication/timeouts.

The cold-start transport test exercises **no cookie → status 401 → login capabilities → login → authenticated status retry**. It tests cookies supplied in either the HTTP header or login XML, using real request preparation, parsing, password hashing and serialization with the HTTP transport mocked. Automated tests complement the owner's hardware confirmation; they do not emulate every firmware behaviour.

## Support and credits

Report lite-fork issues in [Verpz/hikaxpro_hacs_lite](https://github.com/Verpz/hikaxpro_hacs_lite/issues). Include panel model, firmware, Home Assistant version and relevant logs with credentials and personal identifiers removed.

Based on [petrleocompel/hikaxpro_hacs](https://github.com/petrleocompel/hikaxpro_hacs), originally forked from [gunkutzeybek/hikaxpro_hacs](https://github.com/gunkutzeybek/hikaxpro_hacs). Thanks to the upstream authors and contributors. Dormant upstream platform and helper code remains to keep future maintenance practical.
