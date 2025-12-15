"""Device control utilities for Android automation."""

import os
import subprocess
import time
import xml.etree.ElementTree as ET
import tempfile
from typing import List, Optional, Tuple

from phone_agent.config.apps import APP_PACKAGES


def _run_adb(
    cmd: list[str],
    *,
    retries: int = 2,
    timeout: int = 10,
    sleep_s: float = 0.2,
) -> subprocess.CompletedProcess:
    """
    Run an adb command with small retries for stability.

    This keeps existing APIs backward-compatible (functions still return None/bool)
    while reducing flakiness from transient adb issues.
    """
    last: subprocess.CompletedProcess | None = None
    for i in range(max(1, retries + 1)):
        try:
            last = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if last.returncode == 0:
                return last
            # Retry on typical transient errors
            out = (last.stdout or "") + (last.stderr or "")
            transient = any(
                s in out.lower()
                for s in [
                    "device offline",
                    "offline",
                    "timeout",
                    "timed out",
                    "closed",
                    "not found",
                    "no devices",
                    "unauthorized",
                ]
            )
            if not transient:
                return last
        except subprocess.TimeoutExpired as e:
            # Treat as retryable
            last = subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=str(e))
        time.sleep(sleep_s * (i + 1))
    return last or subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="unknown")


def get_ui_hierarchy_compact(
    device_id: str | None = None,
    *,
    max_nodes: int = 180,
    timeout: int = 10,
) -> list[dict[str, str]]:
    """
    Dump UIAutomator hierarchy and return a compact list of nodes.

    This is useful for improving robustness: the model can use visible text/content-desc
    and bounds instead of relying purely on image-based coordinate guessing.
    """
    adb_prefix = _get_adb_prefix(device_id)

    # Dump UI hierarchy on device
    remote_path = "/sdcard/uidump.xml"
    dump = _run_adb(adb_prefix + ["shell", "uiautomator", "dump", remote_path], retries=1, timeout=timeout)
    if dump.returncode != 0:
        return []

    # Pull to a temp file
    local_path = os.path.join(tempfile.gettempdir(), f"uidump_{int(time.time()*1000)}.xml")
    pull = _run_adb(adb_prefix + ["pull", remote_path, local_path], retries=1, timeout=timeout)
    if pull.returncode != 0 or not os.path.exists(local_path):
        return []

    try:
        tree = ET.parse(local_path)
        root = tree.getroot()
        out: list[dict[str, str]] = []

        for node in root.iter():
            if node.tag != "node":
                continue
            text = (node.attrib.get("text") or "").strip()
            desc = (node.attrib.get("content-desc") or "").strip()
            rid = (node.attrib.get("resource-id") or "").strip()
            clazz = (node.attrib.get("class") or "").strip()
            bounds = (node.attrib.get("bounds") or "").strip()

            # Keep nodes that carry semantic signal
            if not (text or desc or rid):
                continue

            out.append(
                {
                    "text": text,
                    "desc": desc,
                    "id": rid,
                    "class": clazz,
                    "bounds": bounds,
                }
            )
            if len(out) >= max_nodes:
                break

        return out
    except Exception:
        return []
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass

def get_current_app(device_id: str | None = None) -> str:
    """
    Get the currently focused app name.

    Args:
        device_id: Optional ADB device ID for multi-device setups.

    Returns:
        The app name if recognized, otherwise "System Home".
    """
    adb_prefix = _get_adb_prefix(device_id)

    result = _run_adb(adb_prefix + ["shell", "dumpsys", "window"], retries=1, timeout=5)
    output = result.stdout

    # Parse window focus info
    for line in output.split("\n"):
        if "mCurrentFocus" in line or "mFocusedApp" in line:
            for app_name, package in APP_PACKAGES.items():
                if package in line:
                    return app_name

    return "System Home"


def tap(x: int, y: int, device_id: str | None = None, delay: float = 1.0) -> None:
    """
    Tap at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after tap.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(
        adb_prefix + ["shell", "input", "tap", str(x), str(y)],
        retries=2,
        timeout=10,
    )
    time.sleep(delay)


def double_tap(
    x: int, y: int, device_id: str | None = None, delay: float = 1.0
) -> None:
    """
    Double tap at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after double tap.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(adb_prefix + ["shell", "input", "tap", str(x), str(y)], retries=2, timeout=10)
    time.sleep(0.1)
    _run_adb(adb_prefix + ["shell", "input", "tap", str(x), str(y)], retries=2, timeout=10)
    time.sleep(delay)


def long_press(
    x: int,
    y: int,
    duration_ms: int = 3000,
    device_id: str | None = None,
    delay: float = 1.0,
) -> None:
    """
    Long press at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        duration_ms: Duration of press in milliseconds.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after long press.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(
        adb_prefix
        + ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
        retries=2,
        timeout=10,
    )
    time.sleep(delay)


def swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration_ms: int | None = None,
    device_id: str | None = None,
    delay: float = 1.0,
) -> None:
    """
    Swipe from start to end coordinates.

    Args:
        start_x: Starting X coordinate.
        start_y: Starting Y coordinate.
        end_x: Ending X coordinate.
        end_y: Ending Y coordinate.
        duration_ms: Duration of swipe in milliseconds (auto-calculated if None).
        device_id: Optional ADB device ID.
        delay: Delay in seconds after swipe.
    """
    adb_prefix = _get_adb_prefix(device_id)

    if duration_ms is None:
        # Calculate duration based on distance
        dist_sq = (start_x - end_x) ** 2 + (start_y - end_y) ** 2
        duration_ms = int(dist_sq / 1000)
        duration_ms = max(1000, min(duration_ms, 2000))  # Clamp between 1000-2000ms

    _run_adb(
        adb_prefix
        + [
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ],
        retries=2,
        timeout=10,
    )
    time.sleep(delay)


def back(device_id: str | None = None, delay: float = 1.0) -> None:
    """
    Press the back button.

    Args:
        device_id: Optional ADB device ID.
        delay: Delay in seconds after pressing back.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(adb_prefix + ["shell", "input", "keyevent", "4"], retries=2, timeout=10)
    time.sleep(delay)


def home(device_id: str | None = None, delay: float = 1.0) -> None:
    """
    Press the home button.

    Args:
        device_id: Optional ADB device ID.
        delay: Delay in seconds after pressing home.
    """
    adb_prefix = _get_adb_prefix(device_id)

    _run_adb(
        adb_prefix + ["shell", "input", "keyevent", "KEYCODE_HOME"], retries=2, timeout=10
    )
    time.sleep(delay)


def launch_app(app_name: str, device_id: str | None = None, delay: float = 1.0) -> bool:
    """
    Launch an app by name.

    Args:
        app_name: The app name (must be in APP_PACKAGES).
        device_id: Optional ADB device ID.
        delay: Delay in seconds after launching.

    Returns:
        True if app was launched, False if app not found.
    """
    if app_name not in APP_PACKAGES:
        return False

    adb_prefix = _get_adb_prefix(device_id)
    package = APP_PACKAGES[app_name]

    _run_adb(
        adb_prefix
        + [
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        retries=2,
        timeout=15,
    )
    time.sleep(delay)
    return True


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]
