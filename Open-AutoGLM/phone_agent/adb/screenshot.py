"""Screenshot utilities for capturing Android device screen."""

import base64
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import Tuple

from PIL import Image


@dataclass
class Screenshot:
    """Represents a captured screenshot."""

    base64_data: str
    width: int
    height: int
    is_sensitive: bool = False


def get_screenshot(device_id: str | None = None, timeout: int = 10) -> Screenshot:
    """
    Capture a screenshot from the connected Android device.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
        timeout: Timeout in seconds for screenshot operations.

    Returns:
        Screenshot object containing base64 data and dimensions.

    Note:
        If the screenshot fails (e.g., on sensitive screens like payment pages),
        a black fallback image is returned with is_sensitive=True.
    """
    temp_path = os.path.join(tempfile.gettempdir(), f"screenshot_{uuid.uuid4()}.png")
    adb_prefix = _get_adb_prefix(device_id)

    try:
        # Execute screenshot command (retry once for flaky adb)
        result = _run_adb(
            adb_prefix + ["shell", "screencap", "-p", "/sdcard/tmp.png"],
            retries=1,
            timeout=timeout,
        )

        # Check for screenshot failure (sensitive screen)
        output = result.stdout + result.stderr
        if "Status: -1" in output or "Failed" in output:
            return _create_fallback_screenshot(is_sensitive=True)

        # Pull screenshot to local temp path
        pull = _run_adb(
            adb_prefix + ["pull", "/sdcard/tmp.png", temp_path],
            retries=1,
            timeout=5,
        )

        if not os.path.exists(temp_path):
            return _create_fallback_screenshot(is_sensitive=False)

        # Read and encode image
        img = Image.open(temp_path)
        width, height = img.size

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

        # Cleanup
        try:
            os.remove(temp_path)
        except OSError:
            pass

        return Screenshot(
            base64_data=base64_data, width=width, height=height, is_sensitive=False
        )

    except Exception as e:
        print(f"Screenshot error: {e}")
        return _create_fallback_screenshot(is_sensitive=False)
    finally:
        # Best-effort cleanup
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]


def _run_adb(cmd: list[str], *, retries: int = 1, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run adb command with light retries for screenshot stability."""
    last: subprocess.CompletedProcess | None = None
    for i in range(max(1, retries + 1)):
        try:
            last = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if last.returncode == 0:
                return last
        except subprocess.TimeoutExpired as e:
            last = subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr=str(e))
        time.sleep(0.2 * (i + 1))
    return last or subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="unknown")


def _create_fallback_screenshot(is_sensitive: bool) -> Screenshot:
    """Create a black fallback image when screenshot fails."""
    default_width, default_height = 1080, 2400

    black_img = Image.new("RGB", (default_width, default_height), color="black")
    buffered = BytesIO()
    black_img.save(buffered, format="PNG")
    base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return Screenshot(
        base64_data=base64_data,
        width=default_width,
        height=default_height,
        is_sensitive=is_sensitive,
    )
