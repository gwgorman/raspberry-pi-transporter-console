# Next Party: Madame Maxime

## Product direction

Madame Maxime's vintage French-circus fortune-telling pavilion is the next
party experience. It replaces Transporter as the party landing page, not as
working code. Guests can still open the complete Transporter console on
request, while Office telemetry remains the practical everyday display.

The intended top-level experiences are:

1. **FORTUNE TELLER** — default in future Party mode.
2. **OFFICE** — default for ordinary unattended operation.
3. **TRANSPORTER** — preserved legacy attraction, selected deliberately.
4. **MAX.sys** — reusable personality and party-safety subsystem rather than a
   discarded one-night feature.

The future selector must not place Transporter in an automatic party rotation.
Opening it is an explicit guest or administrator action. A protected, obvious
route must always return to Madame Maxime or Office without restarting the Pi.

## Preserve these working contracts

The legacy Transporter page keeps its current functionality intact:

- touchscreen ENERGIZE and protected SELF DESTRUCT/ABORT interaction;
- GPIO 17 and GPIO 27 physical controls;
- keyboard/test-mode controls;
- foreground effects, bridge ambience, AFG system volume, and private voice
  override behavior;
- telemetry pages and their failure-isolated data sources;
- kiosk-safe shutdown, restart, and Exit Kiosk controls.

MAX.sys remains available for reuse. Preserve its isolated sidecar process,
validated messages, audio priority, interruption rules, Quiet Watch scheduling,
secret discovery, and private/public voice-asset fallback. Madame Maxime may
reuse those mechanisms but should have her own state, copy, assets, and logs.

## Hardware already planned

- Raspberry Pi 5 and existing 1920x1080 touchscreen
- Arducam B0354 enclosed USB camera (Sony IMX291, 2.8-12 mm varifocal lens)
- Seeed reSpeaker XVF3800 USB four-microphone array with case, without XIAO
- Pololu VL53L1X distance sensor #3415 over I2C
- NEEWER R12L 12-inch ring light
- Existing EntTec Open DMX USB interface and PAR fixtures
- Existing powered speaker connected through AUX for low latency and an
  echo-cancellation-friendly audio path

Exact device names, USB identities, audio routing, camera formats, sensor
address, and light-control capability must be discovered on the real hardware.
Do not hard-code assumptions before it arrives.

### Verified ENTTEC Open DMX USB interface

The ENTTEC adapter was connected to the production Raspberry Pi and identified
on October 19, 2026. Linux binds it to the in-kernel `ftdi_sio` driver as an
FTDI FT232R serial device:

- USB vendor/product: `0403:6001`
- FTDI serial: `BG00UVK9`
- current kernel node: `/dev/ttyUSB0`
- stable application path:
  `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG00UVK9-if00-port0`
- device ownership: `root:plugdev`, mode `0660` plus desktop ACL
- kiosk user groups include both `plugdev` and `dialout`
- PySerial 3.5 is already installed on the Pi

No process currently holds the serial port. Future lighting code must use the
stable `by-id` path, fail silently when it is absent, and never prevent the
touchscreen application from starting. This is an Open DMX interface rather
than a framed DMX USB Pro device: the eventual adapter must continuously emit
the DMX512 break, mark-after-break, start code, and channel frame. Merely
opening the serial port does not prove that a fixture received valid DMX.

The two existing PAR fixtures are configured with DMX starting addresses
**1** and **30**. Treat these as installation defaults. Their selected fixture
mode and exact channel maps must still be verified from the manual and with a
low-level test before assigning color, dimmer, strobe, or macro functions.
The address separation is intentionally generous so their channel footprints
cannot overlap during discovery.

The adapter was enumerated read-only during this check. No DMX values were
transmitted, so the existing kiosk and any connected fixtures were not changed.

## Build safely before the hardware arrives

1. Keep a recoverable snapshot of the known-good deployed kiosk.
2. Add Madame Maxime as a separate module and selectable experience.
3. Use simulated adapters for approach, camera, microphone, and DMX input.
4. Prototype voice conversation with typed-input fallback and record latency.
5. Add crash-safe anonymous persistence at the beginning, not after the party:
   session counts, interaction timing, selected prompts, fortune fingerprints,
   fault/restart events, and coarse performance measurements. Do not retain
   images or transcripts by default.
6. Replace simulated adapters individually as hardware arrives and preserve a
   no-hardware launch path for development.
7. End every development session with the deployed kiosk functioning and back
   in Office mode.

## Guest experience target

Approach wakes the pavilion quickly, a cached greeting begins immediately, and
the guest can start with one oversized touch target. Madame Maxime can use a
visible, optional camera preview to comment on costume or props and can handle
more than one person by asking who wants a reading. Each reading should include
one prediction, one cheeky observation, and one mysterious instruction, with
optional QR delivery of an aged fortune card.

The interface must remain useful in a loud crowd: large touch targets,
hold-to-speak or touchscreen fallback, visible listening/speaking state,
barge-in, bounded sessions, timeouts, and a fast reset for the next guest.
Desired response behavior is approximately 250 ms approach acknowledgement,
500 ms cached greeting start, and one to two seconds from the end of a guest's
speech to the beginning of a streamed reply.

## Lighting intent

The ring light is steady photographic illumination and starts at a low 4400 K
setting. DMX fixtures provide theatrical state cues: dim amber/burgundy at
idle, a gentle approach rise, violet/deep blue during the reading, a brief gold
fortune reveal, and a fade back at farewell. Lighting failure must never block
conversation or core kiosk input.

## Deployment rule

Do not change the boot target to Fortune Teller until its simulated-hardware
path, explicit Transporter access, Office return path, audio routing, and
watchdog recovery have all passed on-Pi testing. Until then the current console
remains the production application.
