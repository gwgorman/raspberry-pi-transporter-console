#!/usr/bin/env python3
"""Failure-isolated Open DMX output for the Madame Maxime pavilion.

The LauceNatz 36LED PAR fixtures use six channels:
master, red, green, blue, strobe, and program.  This module deliberately keeps
strobe and program at zero for every static scene.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from dataclasses import dataclass


DEFAULT_PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG00UVK9-if00-port0"
UNIVERSE_SIZE = 512


def clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


@dataclass(frozen=True)
class ParScene:
    master: int = 0
    red: int = 0
    green: int = 0
    blue: int = 0

    def channels(self) -> tuple[int, int, int, int, int, int]:
        # Channels 5 and 6 are always zero: no strobe, macros, auto, or sound.
        return (
            clamp_byte(self.master),
            clamp_byte(self.red),
            clamp_byte(self.green),
            clamp_byte(self.blue),
            0,
            0,
        )


BLACKOUT = ParScene()
LOW_RED = ParScene(38, 255, 0, 0)
LOW_GREEN = ParScene(38, 0, 255, 0)
LOW_BLUE = ParScene(38, 0, 0, 255)
LOW_WHITE = ParScene(38, 255, 255, 255)
WARM_AMBER = ParScene(42, 255, 72, 8)


class Universe:
    def __init__(self) -> None:
        # Byte zero is the DMX start code; channels 1-512 follow it.
        self._frame = bytearray(UNIVERSE_SIZE + 1)
        self._lock = threading.Lock()

    def set_par(self, address: int, scene: ParScene) -> None:
        if address < 1 or address + 5 > UNIVERSE_SIZE:
            raise ValueError(f"PAR address {address} is outside the DMX universe")
        with self._lock:
            self._frame[address : address + 6] = bytes(scene.channels())

    def blackout(self) -> None:
        with self._lock:
            self._frame[1:] = bytes(UNIVERSE_SIZE)

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._frame)


class OpenDmxOutput:
    """Continuously transmits the current universe through an FTDI Open DMX cable."""

    def __init__(self, universe: Universe, port: str = DEFAULT_PORT) -> None:
        self.universe = universe
        self.port = port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial = None
        self.error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            import serial

            self._serial = serial.Serial(
                self.port,
                baudrate=250000,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_TWO,
                timeout=0,
                write_timeout=0.25,
                exclusive=True,
            )
        except Exception as exc:
            self.error = str(exc)
            raise RuntimeError(f"Unable to open DMX interface {self.port}: {exc}") from exc
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="open-dmx", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self._serial.break_condition = True
                time.sleep(0.00012)
                self._serial.break_condition = False
                time.sleep(0.000012)
                self._serial.write(self.universe.snapshot())
                elapsed = time.monotonic() - started
                if elapsed < 0.025:
                    self._stop.wait(0.025 - elapsed)
        except Exception as exc:
            self.error = str(exc)
            self._stop.set()

    def stop(self, blackout: bool = True) -> None:
        if blackout and self._serial is not None:
            self.universe.blackout()
            time.sleep(0.08)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._serial is not None:
            self._serial.close()
            self._serial = None


def run_test(port: str, hold: float, final_amber: bool) -> None:
    universe = Universe()
    output = OpenDmxOutput(universe, port)
    addresses = (1, 30)
    stopping = False

    def stop_now(_signum=None, _frame=None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop_now)
    signal.signal(signal.SIGTERM, stop_now)

    output.start()
    try:
        steps = [("BLACKOUT", None, BLACKOUT)]
        for address in addresses:
            steps.extend(
                (name, address, scene)
                for name, scene in (
                    ("RED", LOW_RED),
                    ("GREEN", LOW_GREEN),
                    ("BLUE", LOW_BLUE),
                    ("WHITE", LOW_WHITE),
                    ("BLACKOUT", BLACKOUT),
                )
            )
        for name, address, scene in steps:
            if stopping:
                break
            universe.blackout()
            if address is not None:
                universe.set_par(address, scene)
            print(f"{name}: {'all fixtures' if address is None else f'fixture at {address:03d}'}", flush=True)
            time.sleep(hold)
        if final_amber and not stopping:
            universe.blackout()
            for address in addresses:
                universe.set_par(address, WARM_AMBER)
            print("WARM AMBER: both fixtures", flush=True)
            time.sleep(0.5)
            output.stop(blackout=False)
        else:
            output.stop(blackout=True)
    finally:
        if output._serial is not None:
            output.stop(blackout=not final_amber)


def main() -> None:
    parser = argparse.ArgumentParser(description="ENTTEC Open DMX lighting utility")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--test", action="store_true", help="run the two-fixture color test")
    parser.add_argument("--hold", type=float, default=2.0, help="seconds per test color")
    parser.add_argument("--final-amber", action="store_true", help="leave both fixtures dim amber")
    args = parser.parse_args()
    if not args.test:
        parser.error("no action selected; use --test")
    run_test(args.port, max(0.25, args.hold), args.final_amber)


if __name__ == "__main__":
    main()
