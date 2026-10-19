#!/usr/bin/env python3
"""Failure-isolated Open DMX output for the Madame Maxime pavilion.

The LauceNatz 36LED PAR fixtures use six channels:
master, red, green, blue, strobe, and program.  This module deliberately keeps
strobe and program at zero for every static scene.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import signal
import threading
import time
from dataclasses import dataclass


FTDI_VENDOR_ID = 0x0403
FTDI_PRODUCT_ID = 0x6001
DEFAULT_SERIAL = "BG00UVK9"
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
    """Continuously transmit DMX by controlling the FTDI chip directly.

    The Pi's kernel serial path and OLA both produced frames that the installed
    PAR fixtures rejected. Direct libftdi output was verified against the same
    adapter, cable, and fixture after QLC+ proved the physical chain on macOS.
    """

    def __init__(self, universe: Universe, serial_number: str = DEFAULT_SERIAL) -> None:
        self.universe = universe
        self.serial_number = serial_number
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ftdi = None
        self._ftdi_context = None
        self._usb_was_detached = False
        self.error: str | None = None

    @staticmethod
    def _load_ftdi():
        library = ctypes.util.find_library("ftdi1") or "libftdi1.so.2"
        ftdi = ctypes.CDLL(library)
        context = ctypes.c_void_p
        byte_pointer = ctypes.POINTER(ctypes.c_ubyte)
        ftdi.ftdi_new.restype = context
        ftdi.ftdi_free.argtypes = [context]
        ftdi.ftdi_usb_open_desc.argtypes = [
            context, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p
        ]
        ftdi.ftdi_usb_close.argtypes = [context]
        ftdi.ftdi_set_baudrate.argtypes = [context, ctypes.c_int]
        ftdi.ftdi_set_line_property2.argtypes = [
            context, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
        ]
        ftdi.ftdi_set_latency_timer.argtypes = [context, ctypes.c_ubyte]
        ftdi.ftdi_usb_purge_buffers.argtypes = [context]
        ftdi.ftdi_write_data.argtypes = [context, byte_pointer, ctypes.c_int]
        ftdi.ftdi_get_error_string.argtypes = [context]
        ftdi.ftdi_get_error_string.restype = ctypes.c_char_p
        return ftdi

    def _ftdi_error(self, operation: str, result: int) -> RuntimeError:
        detail = self._ftdi.ftdi_get_error_string(self._ftdi_context)
        message = detail.decode(errors="replace") if detail else "unknown libftdi error"
        return RuntimeError(f"{operation} failed ({result}): {message}")

    def _check(self, operation: str, result: int) -> None:
        if result < 0:
            raise self._ftdi_error(operation, result)

    def _claim_usb(self) -> None:
        import usb.core
        import usb.util

        device = usb.core.find(idVendor=FTDI_VENDOR_ID, idProduct=FTDI_PRODUCT_ID)
        if device is None:
            raise RuntimeError("ENTTEC Open DMX USB interface not found")
        self._usb_was_detached = device.is_kernel_driver_active(0)
        if self._usb_was_detached:
            device.detach_kernel_driver(0)
        usb.util.dispose_resources(device)
        time.sleep(0.05)

    def _restore_usb(self) -> None:
        if not self._usb_was_detached:
            return
        try:
            import usb.core
            import usb.util

            device = usb.core.find(idVendor=FTDI_VENDOR_ID, idProduct=FTDI_PRODUCT_ID)
            if device is not None and not device.is_kernel_driver_active(0):
                device.attach_kernel_driver(0)
                usb.util.dispose_resources(device)
        finally:
            self._usb_was_detached = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            self._claim_usb()
            self._ftdi = self._load_ftdi()
            self._ftdi_context = self._ftdi.ftdi_new()
            if not self._ftdi_context:
                raise RuntimeError("libftdi could not allocate a device context")
            description = None
            serial_number = self.serial_number.encode()
            self._check(
                "opening ENTTEC interface",
                self._ftdi.ftdi_usb_open_desc(
                    self._ftdi_context,
                    FTDI_VENDOR_ID,
                    FTDI_PRODUCT_ID,
                    description,
                    serial_number,
                ),
            )
            self._check("setting DMX baud rate", self._ftdi.ftdi_set_baudrate(self._ftdi_context, 250000))
            # libftdi enums: BITS_8=8, STOP_BIT_2=2, NONE=0, BREAK_OFF=0.
            self._check(
                "setting 8N2 line format",
                self._ftdi.ftdi_set_line_property2(self._ftdi_context, 8, 2, 0, 0),
            )
            self._check("setting FTDI latency", self._ftdi.ftdi_set_latency_timer(self._ftdi_context, 1))
            self._check("purging FTDI buffers", self._ftdi.ftdi_usb_purge_buffers(self._ftdi_context))
        except Exception as exc:
            self.error = str(exc)
            self._close_interface()
            raise RuntimeError(f"Unable to open ENTTEC DMX interface: {exc}") from exc
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="open-dmx", daemon=True)
        self._thread.start()

    def _set_break(self, enabled: bool) -> None:
        self._check(
            "setting DMX break",
            self._ftdi.ftdi_set_line_property2(
                self._ftdi_context, 8, 2, 0, 1 if enabled else 0
            ),
        )

    def _write_frame(self, frame: bytes) -> None:
        payload = (ctypes.c_ubyte * len(frame)).from_buffer_copy(frame)
        self._check(
            "writing DMX frame",
            self._ftdi.ftdi_write_data(self._ftdi_context, payload, len(frame)),
        )

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self._set_break(True)
                time.sleep(0.00012)
                self._set_break(False)
                time.sleep(0.000012)
                self._write_frame(self.universe.snapshot())
                elapsed = time.monotonic() - started
                if elapsed < 0.025:
                    self._stop.wait(0.025 - elapsed)
        except Exception as exc:
            self.error = str(exc)
            self._stop.set()

    def _close_interface(self) -> None:
        if self._ftdi_context:
            try:
                self._ftdi.ftdi_usb_close(self._ftdi_context)
            finally:
                self._ftdi.ftdi_free(self._ftdi_context)
                self._ftdi_context = None
        self._ftdi = None
        self._restore_usb()

    def stop(self, blackout: bool = True) -> None:
        if blackout and self._ftdi_context is not None:
            self.universe.blackout()
            time.sleep(0.08)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._close_interface()


def run_test(serial_number: str, hold: float, final_amber: bool) -> None:
    universe = Universe()
    output = OpenDmxOutput(universe, serial_number)
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
        if output._ftdi_context is not None:
            output.stop(blackout=not final_amber)


def main() -> None:
    parser = argparse.ArgumentParser(description="ENTTEC Open DMX lighting utility")
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--test", action="store_true", help="run the two-fixture color test")
    parser.add_argument("--hold", type=float, default=2.0, help="seconds per test color")
    parser.add_argument("--final-amber", action="store_true", help="leave both fixtures dim amber")
    args = parser.parse_args()
    if not args.test:
        parser.error("no action selected; use --test")
    run_test(args.serial, max(0.25, args.hold), args.final_amber)


if __name__ == "__main__":
    main()
