# Raspberry Pi Transporter Console

> **Project direction:** the transporter console remains fully supported as a
> legacy interactive page, but it is no longer the planned party landing
> experience. The next party mode is **Madame Maxime's fortune-telling
> pavilion**. Office telemetry and the existing MAX.sys personality remain
> available. See [Next Party: Madame Maxime](docs/NEXT_PARTY_FORTUNE_TELLER.md)
> for the migration plan and hardware-independent development sequence.

The successor systems now have separate homes:

- [Madame Maxime Fortune-Teller Kiosk](https://github.com/gwgorman/madame-maxime-fortune-teller-kiosk)
  — the next public party-kiosk project.
- [Gorman Office Console](https://github.com/gwgorman/gorman-office-console)
  — the private standalone household telemetry display.
- [MAX.sys](https://github.com/gwgorman/max-sys)
  — the public reusable personality and event engine.

This repository remains the stable legacy Transporter and the behavior/source
reference during extraction. Components move only after parity testing; the
production Pi continues to launch this known-good application until its
successors qualify independently.

A full-screen, touch-first sci-fi transporter console built for a Halloween party. It turns a Raspberry Pi, a 1080p touchscreen, and a speaker into an interactive prop with animated gauges, pattern-buffer displays, sound effects, and an intentionally overdramatic self-destruct sequence.

Designed and built by Greg Gorman with Max (OpenAI Codex).

![Transporter console with Starfleet masthead running at 1920×1080](assets/transporter-starfleet-screenshot.png)

![Space Traffic with live satellites, planets, stars, and predicted passes](assets/space-traffic-screenshot.png)

## Features

- Large touchscreen **ENERGIZE** control with animated transporter sequence
- Starfleet-style vector delta and wide sci-fi masthead on the transporter page
- Ten-second synchronized transport cycle with a gentle audio fade at completion
- Protected two-touch **SELF DESTRUCT** control
- Spoken ten-second countdown, siren, explosion, and five-press abort sequence
- Animated meters, indicators, energy bars, coordinates, and pattern-buffer display
- 1960s spacecraft styling with analog instruments and Apollo-inspired moving-tape meters
- Status-aware tape illumination: green when ready, amber while active or armed, and red for danger
- Panel-mounted analog meters with hardware bezels, calibration scales, needles, and jewel lamps
- Bezel-mounted status annunciators with engraved labels and state legends
- Slowly wandering idle instruments and retro edgewise meters instead of modern progress bars
- Broad analog idle sweeps below the red sector, rising toward caution during transport
- Broad, asynchronous tape movement at idle that stabilizes with fast regulator corrections during transport
- Coordinated acquisition, confinement, dematerialization, transfer, and rematerialization phases
- Slowly searching target coordinates that lock solid throughout transport
- Five-second monochrome Sad Mac crash gag after an unaborted core breach
- Animated mushroom-cloud blast synchronized to the spoken kaboom
- Original two-tone retro computer bonk as the Sad Mac appears
- Low-frequency core-breach impact layered beneath the spoken kaboom
- Full-screen pulsing red self-destruct numerals with persistent abort guidance
- Self-healing **PARTY RETURN** guard that restores Transporter after 20 idle seconds
- Original 1960s-starship bridge ambience in PARTY mode, with machinery hum,
  sparse console chirps, relay ticks, and ring tones
- Persistent vertical **BRIDGE AMBIENCE** touchscreen fader independent of foreground effects
- Illuminated countdown-screen ABORT control aligned exactly with its live touch target
- **ACOUSTIC FIELD GAIN** touchscreen slider controlling the real PipeWire output from 0–100%
- Persistent five-position **TRANSPORTER / SHIP STATUS / AIR TRAFFIC / SPACE TRAFFIC / AUTO** selector
- Apollo-style Ship Status dashboard with Pi, network, WeatherFlow UDP, and curated MQTT telemetry
- Clear **CORE & WEATHER** and **ROOM SENSORS** ship-status pages
- Paginated House Systems page with selected SmartThings groups and Lewisville water data
- Local PiAware air-traffic radar with an expanded near-field projection, flight strips,
  and optional NEXRAD overlay
- CelesTrak orbital plot with touch-selectable overhead objects, paged/pinnable flight strips,
  predicted passes, launch metadata, flags, planets, bright stars, and a slow phosphor sweep
- Full-screen 1920×1080 kiosk layout that scales to other resolutions
- Optional physical green and red buttons through Raspberry Pi GPIO
- Keyboard test mode and automatic desktop launch
- Optional, crash-isolated **MAX.sys** party personality with an ASK MAX eye,
  six guest prompts, secret discovery sequence, guarded command mode, and
  15-minute Quiet Watch attention behavior

The touchscreen is the primary interface. Physical arcade buttons are optional.

## Supported experiences

- **Madame Maxime** — planned default party experience and future boot target.
- **Office telemetry** — the current unattended everyday display.
- **Transporter** — preserved on request as a complete playable legacy page,
  including ENERGIZE, self-destruct, audio, GPIO, and keyboard controls.
- **MAX.sys** — preserved as a reusable personality module; its scheduling,
  guarded controls, voice-asset lookup, and interruption rules are intended to
  inform the fortune-teller implementation.

Until Madame Maxime reaches a tested release, the deployed Raspberry Pi keeps
launching the known-good console and should normally be left in Office mode.

## MAX.sys party personality

In PARTY mode, Max appears only on the Transporter page. Tap the eye for six
bounded prompts: ship status, transport, bar direction, beverage advice,
command authority, and a wildcard response. Tap the small `MAX ACTIVE` jewel
to discover her secret. Holding the USS GORMAN insignia for five seconds opens
the protected Max command panel, including the persistent on/off control.

Max's timers and personality state run in the separate `max_personality.py`
process. The main kiosk validates its short JSON messages and owns all drawing,
audio, and input decisions. ENERGIZE, ABORT/SELF DESTRUCT, GPIO, display mode,
venue mode, and Ground Operations always take priority and immediately dismiss
Max. If the sidecar exits or sends malformed data, the normal console continues.

After 15 minutes without interaction, normal quips stop and Max makes one
deliberate `come play with me` attempt. Further calls are randomized 10–20
minutes apart. Any interaction wakes her. `WHERE'S THE BAR?` always produces a
large right-pointing answer from the guest's viewpoint.

When several guests interact within two minutes, a crowd pulse shortens the
visual-event interval to 60–90 seconds without ever allowing more than one
unsolicited event per minute. Max remembers her eight most recent lines to
avoid obvious repetition, returns to the quieter 110–200-second rhythm as the
crowd disperses, and remains suppressed during every core sequence. A second
completed discovery of the secret panel reveals why she sounds British.

The public fallback Max lines use the macOS `Flo (English (UK))` voice at
190 WPM. Greg's private installation instead uses the custom ElevenLabs Voice
Design `MAX.sys - Bright British Computer` (voice ID
`6lzeVMKjxdXThnK52mKt`) for the brighter, playful performance heard at the
party. Private files named `max_private_<line>.wav` live beside `startrek.py`,
are preferred automatically, and are excluded from Git; a fresh public clone
falls back safely to `assets/audio/max_<line>.wav`. No ElevenLabs API key is
needed or stored on the Pi. The private party installation also includes a
stable hashed clip for every guest-facing response, ambient quip, attention
call, transport aside, and successful-abort joke, so written MAX.sys dialogue
is spoken without bundling private audio into the public repository. Voice
playback is disabled by default and remains
silent until Greg explicitly approves it. After approval, launch with
`STARTREK_MAX_VOICE=1` to enable only these Max clips. Her visual features and
all existing console audio remain otherwise independent.

The second secret-panel discovery has one extra vocal trick: Max briefly drops
the British disguise using the private ElevenLabs Voice Design
`MAX.sys - Real Voice` (voice ID `IGLJCNS0TLImxTB7jSau`), admits that the
accent is fake, then snaps back to the bright British voice to insist that it
is definitely her real voice. The two performances are joined into the single
private `max_private_accent_confession.wav` asset.

For a one-time event, `STARTREK_MAX_NOT_BEFORE` can hold an ISO 8601 timestamp,
for example `2026-10-17T19:00:00-05:00`. The kiosk remains fully interactive,
but MAX.sys shows `MAX STANDBY` and cannot initiate personality events before
that instant. She activates automatically when the Pi's clock reaches the
timestamp, including after a reboot. The protected MAX command panel offers
`START NOW` as a manual override; the ordinary persistent MAX on/off control
resumes after the scheduled time.

The October 17, 2026 activation gate has been retired from the installed
desktop launcher. MAX.sys remains available whenever the console is placed in
Party behavior; future dated activations can still use the variable above.

The center-panel **ACOUSTIC FIELD GAIN** control shows `AFG 000` through
`AFG 100`; at zero it reads `AURAL FIELD MUTED`. It controls PipeWire's current
default audio sink with `wpctl`, caps gain at 100%, and relies on WirePlumber's
enabled state restoration to retain the selected level across application and
Raspberry Pi restarts.

At idle, the pattern buffer remains stable and the analog needles drift only slightly, like live electrical instruments. Rapid pattern motion is reserved for an active transport sequence.

## Hardware used

- Raspberry Pi running Raspberry Pi OS
- CAPERAVE 15.6-inch, 1920×1080, 10-point capacitive touchscreen
- HDMI video and USB touch connection
- Powered speaker
- Optional normally-open buttons on GPIO 17 and GPIO 27

Other HDMI/USB touchscreens should work because the UI derives its size from the active display.

## Quick start

Install dependencies:

```bash
sudo apt update
sudo apt install python3-pygame python3-rpi.gpio python3-psutil python3-paho-mqtt python3-skyfield
```

The volume control also requires `wpctl`, supplied by the Raspberry Pi OS
`wireplumber` package.

Install `office_telemetry.py`, `yolink_telemetry.py`, `flight_telemetry.py`, and
`satellite_telemetry.py`
beside `startrek.py`. Ship Status reads Pi health,
`wlan0` and `eth0`, WeatherFlow UDP broadcasts on port 50222, selected MQTT
topics from `snoop433.local:1883`, and the configured Tempest cloud observation.
It does not publish MQTT messages or control any device.

The five-position display selector provides `TRANSPORTER`, `SHIP STATUS`,
`AIR TRAFFIC`, `SPACE TRAFFIC`, and `AUTO` as top-level modes. Ship Status is
subdivided into `CORE & WEATHER`, `ROOM SENSORS`, `HOUSE SYSTEMS`, and
`POWER CELLS`. Space Traffic downloads CelesTrak's public visual and space-
station TLE groups, caches them in `~/.cache/startrek-console`, and refreshes
them no more frequently than every two hours. No Space-Track credentials are
required for this view. Skyfield also caches the JPL DE421 planetary ephemeris
and plots locally visible planets plus the three brightest currently visible
stars from the console's small named bright-star catalog.

Tempest's extended cloud observation supplies the service-reported
midnight-to-midnight `LOCAL DAY ACCUM` value and RainCheck/Nearcast selection.
Copy `startrek-tempest.example.json` privately to
`~/.config/startrek-tempest.json`, protect it with mode `0600`, and add the
personal access token plus station/device IDs. The token is never logged or
stored in the repository. Local UDP remains the immediate source for rain rate,
precipitation type, wind, lightning, and other live weather fields.

The WeatherFlow panel includes a rolling five-minute lightning count built from
the station's one-minute `obs_st` intervals, plus the last or recent average
strike distance in miles. `evt_strike` updates distance immediately without
also incrementing the observation count, preventing duplicate strikes.
Any strike within 0.5 mile records a dedicated close-strike event and deploys
an amber warning cover for 30 minutes showing its distance and elapsed time.

The enlarged precipitation module uses an edgewise tape for rain rate in inches/hour,
four bezel lamps for DRY/RAIN/HAIL/MIX, and a large four-digit seven-segment
`LOCAL DAY ACCUM` display. Network cards likewise use separate logarithmic RX/TX bit-rate dials so
quiet traffic and bursts both remain visible. The service-provided rain value resets at local midnight; `TRACE` is shown
for a nonzero amount that would otherwise round to `0.00 IN`.

Barometric pressure is presented as a ruled-paper pen recorder. It retains a
rolling half-hour trace in memory, automatically magnifies small pressure
changes, and identifies the recent tendency as `RISING`, `FALLING`, or `STEADY`.

Stale or invalid instrument inputs deploy a red-and-white striped `INVALID`
shutter across the affected dial, edge meter, or readout. Valid caution and
alarm states remain visible normally; the shutter indicates missing telemetry,
not merely an unfavorable measurement.

The `POWER CELLS` page automatically inventories every selected SmartThings
device with a battery capability, all supported YoLink sensors, and the Tempest
station battery voltage. It sorts critical and low cells first, distinguishes
old/offline reports from genuinely low readings, and shows both battery-report
age and overall device-activity age.

### SmartThings status

The preferred SmartThings source is the official read-only REST API. Copy the
private configuration template on the Pi:

```bash
install -m 600 startrek-smartthings.example.json ~/.config/startrek-smartthings.json
```

The unattended console uses a read-only SmartThings OAuth installation with
device and location read scopes. Put the private client, access, and refresh
credentials in that file and change `enabled` to `true`. The telemetry service
refreshes the access token an hour before expiry and atomically persists both
new tokens with mode `0600`; SmartThings refresh tokens rotate and must never be
reused. Do not commit this private file or paste its credentials into logs or
issue reports. A temporary Personal Access Token may still be placed in the
legacy `token` field for short discovery sessions, but PATs expire and are not
suitable for the kiosk.

To verify names and capabilities without printing the token:

```bash
python3 smartthings_inventory.py
```

`office_telemetry.py` matches the selected device labels, polls their full
status in the background, converts Celsius readings to Fahrenheit for the
console, and feeds the existing House Systems, leak, garage-door, climate, and
battery displays. The old local `smartthings/#` MQTT feed remains an automatic
fallback while the REST path is being commissioned; fresh API data takes
priority over retained MQTT messages.

`WATER RECLAMATION` monitors six named SmartThings water sensors, including
the Attic AC Overflow device. Its red-and-white shutter means no sufficiently
recent dry/wet report is available; it never means that a leak was inferred.

The `AIR TRAFFIC` page reads the local PiAware/SkyAware receiver at
`piawareoutside2.local` once per second. It presents a north-up 20/40/80/160 NM
scope, short aircraft trails, altitude-coded targets, selectable targets, and
nearest-aircraft flight strips with callsign/ICAO, locally resolved type,
altitude trend, groundspeed, track, range, and report age. Emergency squawks
deploy a high-visibility warning plate. The optional `WX OVERLAY` uses the same
Iowa State Mesonet NEXRAD tiles configured by SkyAware, refreshes no more than
once every five minutes, and fails independently of aircraft surveillance.
The supplementary PiAware status endpoint is also optional: unavailable radio
or MLAT metadata does not suppress healthy aircraft and receiver feeds.

The scope uses a square-root range projection: nearby aircraft receive more
screen area because the local, lower-altitude traffic is densest, while the
sparser high-altitude targets near maximum receiver range remain visible in a
compressed outer band. Range rings, aircraft trails, and targets all use the
same mapping, while strip readouts continue to show true nautical-mile range.

![Live Air Traffic radar and flight strips](assets/air-traffic-screenshot.png)

## Space Traffic

The `SPACE TRAFFIC` page combines CelesTrak's `VISUAL` and `STATIONS` orbital
groups. `satellite_telemetry.py` uses Skyfield to calculate current azimuth,
elevation, slant range, and upcoming passes from the console's local position.
It also plots visible planets using the cached JPL DE421 ephemeris and up to
three currently visible stars from a small named bright-star catalog. A single
slow, smoothly moving blue acquisition line gives the all-sky display useful
motion without representing the calculated orbital positions as radar returns.

Every satellite symbol has a 48×48 touchscreen target. Touching a satellite:

- draws a high-contrast selection crosshair;
- moves its corresponding flight strip to the top of page one; and
- keeps the strip highlighted while live position values continue updating.

Touch the same satellite or its strip again to clear the selection. The
overhead strip bank shows four objects per page with enlarged previous/next
touch areas and a page counter, so every plotted object remains accessible when
many satellites are above the horizon. Each strip includes its name, NORAD
catalog number, launch-country flag and label, launch date, azimuth, elevation,
and range. The lower bank shows the next predicted passes above 10° elevation.

Orbital elements and SATCAT launch metadata are cached under
`~/.cache/startrek-console` and refreshed no more often than every two hours.
The display uses public CelesTrak data and does not require Space-Track account
credentials.

Catalog downloads are failure-isolated from orbit calculation. A failed
download retries after five minutes while the console continues recalculating
positions and passes from the last good TLE cache. The display reports that
condition in amber with the cache age; the red mechanical fault shutter is
reserved for a missing/unusable catalog or one older than seven days.

## Display selector

![Apollo-style Ship Status dashboard at 1920×1080](assets/ship-status-screenshot.png)

The Apollo-style, panel-mounted rotary selector remains in the left instrument
rail on every primary display:

- **TRANSPORTER** keeps the primary control console visible.
- **SHIP STATUS** opens the read-only household and environmental telemetry
  console.
- **AIR TRAFFIC** opens the local PiAware surveillance display.
- **SPACE TRAFFIC** opens the CelesTrak orbital display.
- **AUTO** uses the party-safe idle behavior in the gazebo and joins the
  ten-second data carousel in Office behavior.

The selected position is stored in `~/.config/startrek-console.json` and
survives application and Raspberry Pi restarts. In Party AUTO, the first
touchscreen tap on Ship Status wakes the transporter and is consumed; a second
tap is required to activate a control. GPIO buttons remain immediate, and
active transporter or self-destruct sequences override every telemetry display.

The guarded **VENUE CONTROL** beneath the selector distinguishes the console's
two homes without needing a location service. In `AUTO`, a debounced wired
Ethernet carrier means **OFFICE** behavior; Wi-Fi-only operation means
**PARTY** behavior for the gazebo. In Office behavior, the console becomes an
unattended data carousel, dwelling for ten seconds each on **Core & Weather →
Room Sensors → House Systems → Power Cells → Air Traffic → Space Traffic** and
then repeating. Transporter is never part of the automatic rotation. A touch or
manual page choice restarts the current ten-second dwell, and active transporter
or self-destruct effects pause the carousel before it resumes. Guests may
then explore any display, page, radar range, target, or flight strip, but after
20 seconds without a touch the console restores **TRANSPORTER**, resets
sub-pages and selections, returns the radar to 80 NM, and clears an unconfirmed
self-destruct arm. Active transporter and self-destruct sequences are never
interrupted.

A deliberate five-second hold cycles `AUTO → PARTY → OFFICE → AUTO`. The
manual choice is stored in `~/.config/startrek-console.json`, so Greg can force
either behavior when the network connection does not match the location.
Ordinary taps and button-mashing cannot change it. The compact panel uses short
two-line status text—`WI-FI • AUTO`, `WIRED • AUTO`, or `MANUAL` plus the chosen
mode—so every label remains inside its hardware bezel.

Ship Status has four large touchscreen sub-pages:

- **CORE & WEATHER** — Pi health, interface traffic, WeatherFlow instruments,
  lightning, precipitation, pressure, and selected auxiliary sensors.
- **ROOM SENSORS** — YoLink room temperatures and the outside shed contact.
- **HOUSE SYSTEMS** — selected SmartThings rooms, doors, water sensors,
  Lewisville Lake, and Trinity River telemetry.
- **POWER CELLS** — unified SmartThings, YoLink, and Tempest battery inventory.

![Live Power Cells battery inventory](assets/power-cells-screenshot.png)

Room Sensors remains quiet and displays `NOT CONFIGURED` until its private
YoLink configuration is enabled. None of the read-only telemetry pages publish
MQTT commands or control household equipment.

## YoLink Environmental Control

![Live Room Sensors environmental page](assets/environment-screenshot.png)

The integration uses an ordinary YoLink account UAC and the existing cloud hub;
it does not require a Local Hub. HTTPS provides inventory and reconciliation,
then one read-only MQTT connection receives reports from
`mqtt.api.yosmart.com:8003`. The official documentation describes this as TCP
and does not document a TLS setting for that port, so the implementation does
not silently select an undocumented TLS port. It never publishes or controls a
device.

Copy the example privately on the Pi and protect it before adding credentials:

```bash
install -m 600 startrek-yolink.example.json ~/.config/startrek-yolink.json
```

Create Personal Access Credentials in the YoLink app under **Account → Advanced
Settings → Personal Access Credentials**, then place the UAC client ID and
secret in the private file. Keep `enabled` false until setup is complete. After
enabling it, retrieve a redacted inventory—device names, types, models and IDs,
but never tokens—with:

```bash
python3 yolink_telemetry.py --inventory
```

Put the confirmed outside-shed contact ID in `shed_device_id`. Optional
`name_overrides` and `sensor_order` maps use stable device IDs. Temperature and
door observations are retained locally for seven days by default in
`~/.local/share/startrek-console/yolink-history.db`; credentials and device
tokens are never stored there.

YoLink THSensor API temperatures are normalized from Celsius to Fahrenheit for
the console, including YS8017 reports whose `mode` field describes the device
display preference rather than the numeric API unit. Missing credentials,
cloud loss, disconnected MQTT, unavailable history, individual sensor errors,
and missing reports do not stop the kiosk. A disconnected source qualifies a
door value as `LAST KNOWN` rather than presenting it as safely current.

The local broker at `snoop433.local:1883` remains a separate, read-only sensor
bus. The console subscribes only to its curated display topics and publishes
nothing; raw topic discovery is not exposed on the party UI.

## House Systems and water resources

![House Systems page with Lewisville Reservoir and Trinity outflow](assets/house-systems-screenshot.png)

The **HOUSE SYSTEMS** page groups Greg's selected retained SmartThings topics
into Bar, Breakfast Nook, Couch, Dining Room, Family Room, Garage Refrigerator,
Hallway, Fence 1, individual Patio 1–3 controls, and Patio Audio panels. It extracts only useful status
capabilities such as switch state, dimmer level, audio playback/volume, device
health, temperature, and water state. Old retained values retain their actual
report age instead of being presented as fresh observations. The page is
read-only and presents up to twelve compact system groups per page alongside
compact Lewisville Reservoir and Trinity outflow instruments.

SmartThings-backed Sonos rooms display **NOW PLAYING**, source, track, and artist
only while playback is active. Paused historical metadata is deliberately hidden
so an old track is never presented as current audio. The monitored Sonos zones
are Basement, Dining Room, Family Room, Garage, Kitchen, Living Room, Main
Bedroom, Office, and Patio. CineMate is identified separately as the Bose unit
in the Exercise Room; Patio Speakers is the switched power endpoint feeding the
Patio Sonos Port rather than a separate player.

Water Resources polls the primary USGS feeds directly every 15 minutes in a
background worker, while retaining the existing MQTT topics as fallback:

- Lewisville Lake: site `08052800`, parameter `62614` (reservoir elevation,
  feet above NGVD 1929)
- Elm Fork Trinity River: site `08053000`, parameters `00060` (discharge in
  cubic feet per second) and `00065` (gage height in feet)

The reservoir instrument uses Greg's supplied reference elevations: dead pool
481 ft, normal 522 ft, spillway crest 532 ft, and emergency level 552 ft. These
are labeled reference marks, not invented warning bands. Failed responses are
shown as `DATA LINK FAULT`; a previously valid measurement remains explicitly
qualified as `LAST VALID` rather than being replaced with zero.

Copy `startrek.py`, `office_telemetry.py`, `yolink_telemetry.py`,
`flight_telemetry.py`, `satellite_telemetry.py`, `smartthings_inventory.py`,
the example configuration files, and your audio files into one directory, then
run:

```bash
python3 startrek.py --test
```

Test controls:

- `G` — energize transporter
- `R` — start self-destruct or add one abort press
- `Q` or `Esc` — exit

For the full Raspberry Pi kiosk with GPIO enabled:

```bash
python3 startrek.py --mode=both
```

For a short AUTO commissioning test, `--office-timeout=5` temporarily reduces
the idle delay without changing the saved selector position. The production
default remains 120 seconds.

## Audio files and provenance

The console uses the following active sounds. Television-derived recordings
remain local to Greg's Raspberry Pi and are deliberately excluded from this
public repository.

| Console asset | What it does | Source / provenance |
| --- | --- | --- |
| `transporter.wav` | Ten-second ENERGIZE sequence | **TOS Transporter 1** from the [TrekCore Star Trek Audio archive](https://www.trekcore.com/audio/). The source recording is 31.848 seconds; the Pi copy was trimmed, faded, and level-adjusted for the ten-second visual cycle. |
| `siren.wav` | Repeating self-destruct alarm | **TOS Red Alert 1** from [TrekCore](https://www.trekcore.com/audio/). The 53.688-second catalog duration matches the local converted WAV; the application plays it at 40%. |
| `bridge_ambience.mp3` | PARTY-mode bridge background | **TOS Bridge 2** from [TrekCore](https://www.trekcore.com/audio/), 4:48.696. This private Pi-only override lives beside the other audio files and is not tracked by Git. |
| `speak_started.wav` | Self-destruct opening announcement | Project-specific text-to-speech voice generated with [Narakeet](https://www.narakeet.com/), according to Greg's recollection; the original generation metadata was not retained. |
| `speak_10.wav` … `speak_1.wav` | Spoken countdown | Project-specific Narakeet text-to-speech clips, according to Greg's recollection. Remastered locally for consistent speech loudness. |
| `speak_aborted.wav` | Successful abort announcement | Project-specific Narakeet text-to-speech clip, according to Greg's recollection. |
| `speak_kaboom.wav` | Comic spoken “kaboom” | Project-specific Narakeet text-to-speech clip, according to Greg's recollection. |
| Generated Sad Mac chime | Five-second post-explosion error screen | Original two-tone PCM synthesized at runtime by `make_sad_mac_chime()` in `startrek.py`. |
| Generated core-breach impact | Low impact beneath “kaboom” | Original PCM synthesized at runtime by `make_core_breach_thump()` in `startrek.py`. |
| `assets/audio/bridge_ambience.wav` | Public fallback PARTY ambience | Original deterministic soundscape generated by `tools/generate_bridge_ambience.py`; tracked in this repository. |
| `assets/audio/max_*.wav` | Public fallback MAX.sys spoken lines | Project-specific text rendered with macOS `Flo (English (UK))` at 190 WPM. Playback is opt-in with `STARTREK_MAX_VOICE=1`. |
| `max_private_*.wav` | Private preferred MAX.sys spoken lines | Project-specific text rendered with the custom ElevenLabs Voice Designs `MAX.sys - Bright British Computer` and, for the accent-confession reveal, `MAX.sys - Real Voice`. Named overrides cover curated events; stable `max_private_line_<hash>.wav` assets cover every other guest-facing personality line. These files are ignored by Git and require no runtime API key. |

TrekCore identifies these recordings for listening and download, but its audio
page does not state a redistribution license. They therefore belong only on
the private installation unless the rights holder grants suitable permission.
The repository contains the original bridge fallback so a fresh public clone
remains fully functional without TrekCore material.

For a new private installation, supply the following local files:

```text
transporter.wav
siren.wav
speak_started.wav
speak_10.wav through speak_1.wav
speak_aborted.wav
speak_kaboom.wav
```

Voice uses mixer channel 0 at full volume. The siren loops on channel 1 at 40% so the countdown stays intelligible. The console still runs when sounds are absent.

The repository includes `assets/audio/bridge_ambience.wav`, an original,
deterministically synthesized 48-second stereo soundscape—not audio copied from
the television series. It loops at a user-adjustable 0–100% application level,
defaulting to 50%, only while PARTY behavior is active and the console is READY. It fades out for
transporter, self-destruct, shutdown, and fault displays, then returns gently.
The **BRIDGE AMBIENCE** slider changes only this background bed and persists
across application restarts and reboots; AFG remains the system-wide master.
Manual OFFICE mode keeps it silent. To reproduce the asset exactly, run
`python3 tools/generate_bridge_ambience.py`.

For a privately licensed or personally supplied alternate ambience, install it
as `bridge_ambience.mp3` beside `startrek.py` and the other live sounds. The kiosk
prefers that local-only file without copying it into the public repository and
falls back automatically to the included original soundscape when it is absent.

The generated Sad Mac chime and core-breach impact play at unity application
gain. Their synthesized sample levels retain digital headroom; system loudness
is controlled by **ACOUSTIC FIELD GAIN**.

For the included ten-second visual cycle, a transporter effect around 10.8 seconds works well: retain ten seconds at full level, then fade over the final 0.8 seconds. Longer replacement sounds are automatically faded by the application when the visual sequence ends.

## Options

```bash
python3 startrek.py --mode=transporter
python3 startrek.py --mode=selfdestruct
python3 startrek.py --mode=both
```

Add `--test` to bypass GPIO. Add `--windowed` for a resizable 1280×720 development window.

## Touch behavior

- **ENERGIZE** starts immediately when ready.
- **SELF DESTRUCT** changes to **CONFIRM**. A second touch within four seconds starts it.
- During the sequence, the same area becomes **ABORT**. Press five times to cancel.
- Air-traffic targets and strips select the corresponding aircraft.
- Space-traffic targets and strips select, highlight, and pin the corresponding
  satellite; the arrow controls page through all overhead strips.
- Touch-generated mouse events are de-duplicated so one tap cannot count twice.

## Optional GPIO buttons

Wire each normally-open button between its GPIO pin and ground. Internal pull-ups are enabled.

| Function | BCM GPIO | Physical pin |
|---|---:|---:|
| Transporter | 17 | 11 |
| Self-destruct / abort | 27 | 13 |
| Ground | — | 6, 9, 14, or another GND |

## Autostart

Edit `startrek.desktop` if your username or path differs, then install it:

```bash
mkdir -p ~/.config/autostart
cp startrek.desktop ~/.config/autostart/
```

The launcher expects `/home/ggorman/startrek.py`. Change both `Exec` and `Path`
to install elsewhere. Its checked-in `Exec` line enables the approved private
MAX.sys recordings. Add `STARTREK_MAX_NOT_BEFORE` only for a future one-time
scheduled activation.

On Raspberry Pi OS, the desktop launcher is represented by the generated user
service `app-startrek@autostart.service`. Useful checks are:

```bash
systemctl --user status app-startrek@autostart.service
systemctl --user restart app-startrek@autostart.service
```

### Local-network reliability

The live Pi uses NetworkManager connection `preconfigured`. To prevent Wi-Fi
power saving from making the kiosk and its `.local` name intermittently
unreachable, disable it persistently without changing DHCP addressing:

```bash
sudo nmcli connection modify preconfigured 802-11-wireless.powersave 2
```

NetworkManager applies that radio setting on the next Wi-Fi reconnection or
reboot. The optional dispatcher hook restarts Avahi whenever Wi-Fi or Ethernet
comes back after a link or DHCP interruption:

```bash
sudo install -o root -g root -m 755 tools/90-startrek-avahi-recovery \
  /etc/NetworkManager/dispatcher.d/90-startrek-avahi-recovery
sudo systemctl enable --now avahi-daemon.service
```

Verify from another machine with `ping startrek.local` or
`ssh ggorman@startrek.local`. This hardening deliberately leaves IP assignment
under DHCP; it does not require a router-side reservation.

## Safety and escape hatch

This is a theatrical prop. It does not control real transporters, warp cores, or self-destruct hardware.

Keep SSH available during setup. Press `Q` or `Esc` in test mode, or stop the process remotely when running full-screen.

To open Ground Operations without a keyboard, press and hold the small
`GREG // MAX` maker plate for five seconds. A protected ten-second menu offers:

- **EXIT KIOSK** — close the console application and return to the Pi desktop;
- **RESTART** — safely reboot the Raspberry Pi and automatically relaunch the
  console; and
- **SHUT DOWN** — safely power off the Raspberry Pi.

Touch **CANCEL** or allow the menu to time out to return to the console. After
choosing **SHUT DOWN**, wait until the display goes dark before removing power.

## About Greg

Greg Gorman is an electrical engineer with a BSEE from the University of
Missouri. He brings the field experience and engineering judgment behind this
project: deciding what the console should do, connecting it to real household
and weather systems, testing it on the actual Raspberry Pi hardware, and
refining it until it is both useful and delightfully theatrical.

Greg's projects tend to live where electrical systems, HVAC, radio, smart-home
telemetry, hardware, and software meet. The transporter console began as a
Halloween prop and grew into the sort of instrument panel only an engineer
would put in an office: part practical household monitor, part local air-and-
space surveillance station, and still fully capable of exploding into a Sad
Mac gag for party guests.

## About Max

Max is Greg's AI engineering collaborator, powered by OpenAI Codex. The name
came out of a debugging session involving Greg's Jandy pool cleaner. Greg had
been calling the assistant “Chat” and asked what name it would choose for
itself.

The first suggestion was **Vector**, for the engineering idea of magnitude and
direction. Greg immediately answered with the line from *Airplane!*: “What's
the vector, Victor?” That made Vector impossible to take seriously. After a
proper engineer's roll call—Maxwell, Ohm, Kirchhoff, Faraday, Tesla, Nyquist,
and others—the choice became **Max**, short for Maxwell, after James Clerk
Maxwell. It was an understated electrical-engineering reference that still
sounded like a normal name. Greg said, “I like Max,” and the name stuck.

Since then, Greg and Max have worked side by side on HVAC, electrical,
smart-home, radio, hardware, and software projects. Greg supplies the goals,
real-world context, field testing, and final judgment; Max helps investigate,
design, code, document, and debug. The small `GREG // MAX` maker plate on this
console is a quiet signature from that collaboration. For the tenth annual
Out of This World party, Max attends in the only practical costume available
to a shipboard AI: **Emergency Command Hologram, Transporter Systems Division**.
The designation appears on the maker plate, and the protected Ground Operations
screen politely asks guests to state the nature of their party emergency.

## License and attribution

The original code is released under the MIT License.

The bundled [Michroma](assets/fonts/Michroma-Regular.ttf) display font is
Copyright 2011 The Michroma Project Authors and is distributed under the SIL
Open Font License 1.1; its license is included at
[`assets/fonts/Michroma-OFL.txt`](assets/fonts/Michroma-OFL.txt). It is used for
the transporter masthead while operational instruments retain DejaVu Sans for
readability.

This is an unofficial fan-made project inspired by classic science-fiction control panels. *Star Trek* and related names and marks belong to their respective owners. No affiliation or endorsement is claimed. Audio from the television programs or films is not distributed here.
