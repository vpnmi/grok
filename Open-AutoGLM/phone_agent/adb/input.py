"""Input utilities for Android device text input."""

import base64
import subprocess
import time
from typing import Optional


def _run_adb(cmd: list[str], *, retries: int = 2, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run adb command with light retries for stability."""
    last: subprocess.CompletedProcess | None = None
    for i in range(max(1, retries + 1)):
        try:
            last = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if last.returncode == 0:
                return last
        except subprocess.TimeoutExpired as e:
            last = subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=str(e))
        time.sleep(0.15 * (i + 1))
    return last or subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="unknown")


def type_text(text: str, device_id: str | None = None) -> None:
    """
    Type text into the currently focused input field using ADB Keyboard.

    Args:
        text: The text to type.
        device_id: Optional ADB device ID for multi-device setups.

    Note:
        Requires ADB Keyboard to be installed on the device.
        See: https://github.com/nicnocquee/AdbKeyboard
    """
    adb_prefix = _get_adb_prefix(device_id)
    encoded_text = base64.b64encode(text.encode("utf-8")).decode("utf-8")

    _run_adb(
        adb_prefix
        + [
            "shell",
            "am",
            "broadcast",
            "-a",
            "ADB_INPUT_B64",
            "--es",
            "msg",
            encoded_text,
        ],
        retries=2,
        timeout=10,
    )


def clear_text(device_id: str | None = None) -> None:
    """
    Clear text in the currently focused input field.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(
        adb_prefix + ["shell", "am", "broadcast", "-a", "ADB_CLEAR_TEXT"],
        retries=2,
        timeout=10,
    )


def detect_and_set_adb_keyboard(device_id: str | None = None) -> str:
    """
    Detect current keyboard and switch to ADB Keyboard if needed.

    Args:
        device_id: Optional ADB device ID for multi-device setups.

    Returns:
        The original keyboard IME identifier for later restoration.
    """
    adb_prefix = _get_adb_prefix(device_id)

    # Get current IME
    result = _run_adb(
        adb_prefix + ["shell", "settings", "get", "secure", "default_input_method"],
        retries=1,
        timeout=5,
    )
    current_ime = (result.stdout + result.stderr).strip()

    # Switch to ADB Keyboard if not already set
    if "com.android.adbkeyboard/.AdbIME" not in current_ime:
        _run_adb(
            adb_prefix + ["shell", "ime", "set", "com.android.adbkeyboard/.AdbIME"],
            retries=2,
            timeout=10,
        )

    # Warm up the keyboard
    type_text("", device_id)

    return current_ime


def restore_keyboard(ime: str, device_id: str | None = None) -> None:
    """
    Restore the original keyboard IME.

    Args:
        ime: The IME identifier to restore.
        device_id: Optional ADB device ID for multi-device setups.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(adb_prefix + ["shell", "ime", "set", ime], retries=2, timeout=10)


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]
