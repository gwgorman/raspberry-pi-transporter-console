#!/usr/bin/env python3
"""Generate the console's original 1960s-starship bridge ambience."""

import math
import random
import struct
import wave
from pathlib import Path

RATE = 22050
DURATION = 48.0
OUTPUT = Path(__file__).resolve().parents[1] / "assets" / "audio" / "bridge_ambience.wav"


def envelope(t, start, duration, attack=.025, decay=3.5):
    local = t - start
    if local < 0 or local >= duration:
        return 0.0
    return min(1.0, local / attack) * math.exp(-decay * local / duration)


def main():
    rng = random.Random(1701)
    events = []
    cursor = 1.2
    kinds = ("blip", "blip", "double", "chirp", "relay", "ring")
    while cursor < DURATION - 1.5:
        kind = rng.choice(kinds)
        events.append((cursor, kind, rng.uniform(0.82, 1.18), rng.uniform(-0.7, 0.7)))
        cursor += rng.uniform(1.35, 3.6)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    peak = 0.0
    frames = bytearray()
    noise_state = 0.0
    for index in range(round(RATE * DURATION)):
        t = index / RATE
        # A restrained machinery bed: transformer hum, motor harmonics, and air.
        slow = 0.88 + 0.12 * math.sin(2 * math.pi * t / 12.0)
        bed = slow * (0.025 * math.sin(2 * math.pi * 55 * t) +
                      0.014 * math.sin(2 * math.pi * 82.5 * t) +
                      0.008 * math.sin(2 * math.pi * 110 * t))
        noise_state = noise_state * .985 + rng.uniform(-1, 1) * .015
        left = right = bed + noise_state * .014

        for start, kind, pitch, pan in events:
            local = t - start
            signal = 0.0
            if kind == "blip" and 0 <= local < .12:
                signal = .22 * envelope(t, start, .12, .008, 5) * math.sin(2 * math.pi * 1120 * pitch * local)
            elif kind == "double":
                for offset, frequency in ((0, 880), (.14, 1320)):
                    if 0 <= local - offset < .085:
                        signal += .17 * envelope(t, start + offset, .085, .006, 5) * math.sin(2 * math.pi * frequency * pitch * (local - offset))
            elif kind == "chirp" and 0 <= local < .28:
                phase = 2 * math.pi * (620 * local + 1050 * local * local)
                signal = .15 * envelope(t, start, .28, .018, 2.8) * math.sin(phase * pitch)
            elif kind == "relay" and 0 <= local < .055:
                signal = .10 * envelope(t, start, .055, .002, 8) * rng.uniform(-1, 1)
            elif kind == "ring" and 0 <= local < .55:
                signal = .11 * envelope(t, start, .55, .012, 4) * (
                    math.sin(2 * math.pi * 470 * pitch * local) +
                    .45 * math.sin(2 * math.pi * 705 * pitch * local))
            if signal:
                left += signal * (1.0 - max(0.0, pan) * .42)
                right += signal * (1.0 + min(0.0, pan) * .42)

        peak = max(peak, abs(left), abs(right))
        frames.extend(struct.pack("<hh", int(max(-1, min(1, left)) * 32767),
                                  int(max(-1, min(1, right)) * 32767)))

    with wave.open(str(OUTPUT), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(frames)
    print(f"wrote {OUTPUT} ({DURATION:.0f}s, peak {20 * math.log10(peak):.1f} dBFS)")


if __name__ == "__main__":
    main()
