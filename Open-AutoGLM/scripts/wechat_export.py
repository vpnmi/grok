#!/usr/bin/env python3
"""
WeChat chat exporter (non-invasive).

Goal:
- Export chat history for a contact by scrolling and saving:
  1) screenshot PNG
  2) UIAutomator hierarchy (compact JSON)

Notes:
- This does NOT decrypt WeChat databases and does NOT bypass captcha.
- If a verification/captcha page is detected, it will pause for manual takeover.

Usage:
  python scripts/wechat_export.py --contact "张三" --out ./exports/wechat --pages 80
  python scripts/wechat_export.py --contacts-file contacts.txt --out ./exports/wechat
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from dataclasses import dataclass

from phone_agent.adb import (
    get_current_app,
    get_screenshot,
    get_ui_hierarchy_compact,
    launch_app,
    swipe,
    tap,
    type_text,
    detect_and_set_adb_keyboard,
    restore_keyboard,
)


CAPTCHA_KEYWORDS = [
    "验证码",
    "校验码",
    "安全验证",
    "安全校验",
    "滑动验证",
    "请完成验证",
    "请先验证",
    "人机验证",
    "图形验证",
    "短信验证码",
    "发送验证码",
    "获取验证码",
    "请拖动滑块",
    "拖动滑块",
    "captcha",
    "verify",
    "verification",
    "recaptcha",
]


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


def _decode_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    # Format: [x1,y1][x2,y2]
    m = re.match(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$", bounds.strip())
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return x1, y1, x2, y2


def _center(bounds: str) -> tuple[int, int] | None:
    b = _decode_bounds(bounds)
    if not b:
        return None
    x1, y1, x2, y2 = b
    return (x1 + x2) // 2, (y1 + y2) // 2


def _find_first_node(ui: list[dict[str, str]], *, contains: str) -> dict[str, str] | None:
    needle = contains.strip().lower()
    if not needle:
        return None
    for n in ui:
        blob = " ".join(
            [
                (n.get("text") or ""),
                (n.get("desc") or ""),
                (n.get("id") or ""),
                (n.get("class") or ""),
            ]
        ).lower()
        if needle in blob:
            return n
    return None


def _ui_has_captcha(ui: list[dict[str, str]]) -> tuple[bool, str]:
    for kw in CAPTCHA_KEYWORDS:
        n = _find_first_node(ui, contains=kw)
        if n:
            evidence = (n.get("text") or n.get("desc") or n.get("id") or "").strip()
            return True, f"keyword='{kw}', evidence='{evidence}'"
    return False, ""


def _screen_fp(screenshot_b64: str, current_app: str) -> str:
    return f"{current_app}|{(screenshot_b64 or '')[:512]}"


@dataclass
class ExportPage:
    idx: int
    screenshot_path: str
    ui_path: str
    current_app: str


def _save_page(
    *,
    out_dir: str,
    idx: int,
    screenshot_b64: str,
    ui_nodes: list[dict[str, str]],
    meta: dict,
) -> ExportPage:
    _ensure_dir(out_dir)

    png_path = os.path.join(out_dir, f"page_{idx:04d}.png")
    ui_path = os.path.join(out_dir, f"page_{idx:04d}.ui.json")
    meta_path = os.path.join(out_dir, f"page_{idx:04d}.meta.json")

    with open(png_path, "wb") as f:
        f.write(base64.b64decode(screenshot_b64))

    with open(ui_path, "w", encoding="utf-8") as f:
        json.dump(ui_nodes, f, ensure_ascii=False, indent=2)

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return ExportPage(
        idx=idx,
        screenshot_path=png_path,
        ui_path=ui_path,
        current_app=str(meta.get("current_app") or ""),
    )


def _tap_node_center(node: dict[str, str], *, device_id: str | None) -> bool:
    c = _center(node.get("bounds") or "")
    if not c:
        return False
    x, y = c
    tap(x, y, device_id=device_id)
    return True


def _enter_chat_by_search(
    *,
    device_id: str | None,
    contact: str,
    screenshot_w: int,
    screenshot_h: int,
    verbose: bool,
) -> bool:
    """
    Best-effort:
    - Ensure in WeChat
    - Tap top search bar / search entry
    - Type contact
    - Tap a search result that contains the contact name
    """
    # Try to tap the "微信" tab at the bottom (Chats)
    ui = get_ui_hierarchy_compact(device_id, max_nodes=220)
    tab = _find_first_node(ui, contains="微信")
    if tab:
        _tap_node_center(tab, device_id=device_id)
        time.sleep(1.0)

    # Find search entry
    ui = get_ui_hierarchy_compact(device_id, max_nodes=260)
    search = _find_first_node(ui, contains="搜索")
    if not search:
        # Fallback: tap near top search bar region
        tap(int(screenshot_w * 0.5), int(screenshot_h * 0.08), device_id=device_id)
        time.sleep(0.8)
    else:
        _tap_node_center(search, device_id=device_id)
        time.sleep(0.8)

    # Type contact name using ADB keyboard
    original_ime = detect_and_set_adb_keyboard(device_id)
    time.sleep(0.6)
    type_text(contact, device_id)
    time.sleep(1.2)
    restore_keyboard(original_ime, device_id)
    time.sleep(0.6)

    # Tap search result
    ui = get_ui_hierarchy_compact(device_id, max_nodes=320)
    result = _find_first_node(ui, contains=contact)
    if result and _tap_node_center(result, device_id=device_id):
        time.sleep(1.2)
        return True

    if verbose:
        print(f"[WARN] Could not find search result for contact: {contact}")
    return False


def export_contact(
    *,
    device_id: str | None,
    contact: str,
    out_root: str,
    pages: int,
    no_change_stop: int,
    verbose: bool,
) -> None:
    # Prepare output
    safe_contact = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_\-]+", "_", contact).strip("_")
    out_dir = os.path.join(out_root, safe_contact)
    _ensure_dir(out_dir)

    # Get initial screen size
    s = get_screenshot(device_id)
    w, h = s.width, s.height

    # Navigate into the chat
    ok = _enter_chat_by_search(
        device_id=device_id,
        contact=contact,
        screenshot_w=w,
        screenshot_h=h,
        verbose=verbose,
    )
    if not ok:
        if verbose:
            print(f"[ERROR] Failed to open chat for: {contact}")
        return

    last_fp = ""
    no_change = 0

    for i in range(pages):
        screenshot = get_screenshot(device_id)
        current_app = get_current_app(device_id)
        ui = get_ui_hierarchy_compact(device_id, max_nodes=500)

        # Captcha takeover (pause)
        is_captcha, reason = _ui_has_captcha(ui)
        if is_captcha:
            print(f"[TAKEOVER] Captcha/verification detected ({reason}).")
            input("请在手机上手动完成验证后，按回车继续...")
            time.sleep(1.0)
            # After takeover, continue loop without consuming a page index
            continue

        fp = _screen_fp(screenshot.base64_data, current_app)
        meta = {
            "ts": time.time(),
            "contact": contact,
            "page": i,
            "current_app": current_app,
            "width": screenshot.width,
            "height": screenshot.height,
            "is_sensitive": screenshot.is_sensitive,
        }

        _save_page(
            out_dir=out_dir,
            idx=i,
            screenshot_b64=screenshot.base64_data,
            ui_nodes=ui,
            meta=meta,
        )

        if fp == last_fp:
            no_change += 1
        else:
            no_change = 0
        last_fp = fp

        if no_change_stop > 0 and no_change >= no_change_stop:
            if verbose:
                print(f"[INFO] No-change threshold reached ({no_change_stop}), stopping: {contact}")
            break

        # Swipe up to load older messages
        start_x = int(w * 0.5)
        start_y = int(h * 0.78)
        end_x = int(w * 0.5)
        end_y = int(h * 0.22)
        swipe(start_x, start_y, end_x, end_y, device_id=device_id)
        time.sleep(1.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device-id", type=str, default=None, help="ADB device id (optional)")
    ap.add_argument("--out", type=str, required=True, help="Output root dir")
    ap.add_argument("--pages", type=int, default=80, help="Max pages (screens) per contact")
    ap.add_argument(
        "--no-change-stop",
        type=int,
        default=3,
        help="Stop after N consecutive identical screens (0 disables)",
    )
    ap.add_argument("--contact", type=str, default=None, help="Single contact name")
    ap.add_argument("--contacts-file", type=str, default=None, help="File with contacts, one per line")
    ap.add_argument("--verbose", action="store_true", help="Verbose logs")
    args = ap.parse_args()

    if not args.contact and not args.contacts_file:
        raise SystemExit("Need --contact or --contacts-file")

    _ensure_dir(args.out)

    # Launch WeChat
    if args.verbose:
        print("[INFO] Launching WeChat...")
    launch_app("微信", args.device_id)
    time.sleep(2.0)

    contacts: list[str] = []
    if args.contact:
        contacts.append(args.contact.strip())
    if args.contacts_file:
        with open(args.contacts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                contacts.append(line)

    for c in contacts:
        if args.verbose:
            print(f"[INFO] Exporting contact: {c}")
        export_contact(
            device_id=args.device_id,
            contact=c,
            out_root=args.out,
            pages=args.pages,
            no_change_stop=args.no_change_stop,
            verbose=args.verbose,
        )
        # Small pause between contacts
        time.sleep(1.0)

    if args.verbose:
        print("[DONE] Export completed.")


if __name__ == "__main__":
    main()

