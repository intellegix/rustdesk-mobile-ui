#!/usr/bin/env python3
"""
RustDesk Mobile UI - Relay Client (Runs on your PC)
Connects to the relay server and executes commands locally.
Supports live window streaming.
"""

import asyncio
import json
import os
import sys
import argparse
from urllib.parse import urljoin
from typing import Dict, Set

import aiohttp

# Import the local server functions
from server import (
    load_app_config, get_window_list, get_volume, set_volume,
    get_brightness, set_brightness, get_system_info, get_rustdesk_status,
    HAS_WIN32, HAS_PYCAW, HAS_SBC
)

# Import window capture
try:
    from window_capture import WindowCapture, ChromeController
    HAS_CAPTURE = WindowCapture.is_available()
except ImportError:
    HAS_CAPTURE = False
    WindowCapture = None
    ChromeController = None

import subprocess
import ctypes
from pathlib import Path
from datetime import datetime
import pyperclip

# Try Windows imports
try:
    import win32gui
    import win32con
except ImportError:
    pass


class RelayClient:
    def __init__(self, relay_url: str, auth_token: str):
        self.relay_url = relay_url.rstrip('/')
        self.auth_token = auth_token
        self.ws = None
        self.running = False
        self.active_streams: Dict[str, asyncio.Task] = {}  # window_id -> capture task
        self.stream_options: Dict[str, dict] = {}  # window_id -> {fps, quality, max_width}

    async def handle_request(self, request_id: str, endpoint: str, method: str, data: dict = None) -> dict:
        """Handle an incoming request from the relay."""
        try:
            # Route to appropriate handler
            if endpoint == "/api/apps" and method == "GET":
                apps = load_app_config()
                return {"apps": sorted(apps, key=lambda x: x.get("priority", 99))}

            elif endpoint == "/api/windows" and method == "GET":
                return {"windows": get_window_list()}

            elif endpoint.startswith("/api/launch/") and method == "POST":
                app_id = endpoint.split("/")[-1]
                apps = load_app_config()
                app_config = next((a for a in apps if a["id"] == app_id), None)
                if not app_config:
                    return {"error": "App not found"}
                subprocess.Popen(
                    app_config["command"],
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                )
                return {"status": "launched", "app": app_config["name"]}

            elif endpoint == "/api/launch-custom" and method == "POST":
                command = data.get("command", "") if data else ""
                if command:
                    subprocess.Popen(
                        command,
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                    )
                return {"status": "launched"}

            elif endpoint.startswith("/api/windows/") and "/focus" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    if win32gui.IsIconic(hwnd):
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                return {"status": "focused"}

            elif endpoint.startswith("/api/windows/") and "/close" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return {"status": "closed"}

            elif endpoint.startswith("/api/windows/") and "/minimize" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                return {"status": "minimized"}

            elif endpoint.startswith("/api/windows/") and "/maximize" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                return {"status": "maximized"}

            elif endpoint == "/api/system/volume" and method == "GET":
                return get_volume()

            elif endpoint == "/api/system/volume" and method == "POST":
                level = data.get("level", 50) if data else 50
                return set_volume(level)

            elif endpoint == "/api/system/volume/mute" and method == "POST":
                if HAS_PYCAW:
                    from pycaw.pycaw import AudioUtilities
                    speakers = AudioUtilities.GetSpeakers()
                    volume = speakers.EndpointVolume
                    current_mute = volume.GetMute()
                    volume.SetMute(not current_mute, None)
                    return {"status": "toggled", "muted": not current_mute}
                return {"error": "pycaw not available"}

            elif endpoint == "/api/system/brightness" and method == "GET":
                return get_brightness()

            elif endpoint == "/api/system/brightness" and method == "POST":
                level = data.get("level", 100) if data else 100
                return set_brightness(level)

            elif endpoint == "/api/clipboard" and method == "GET":
                try:
                    content = pyperclip.paste()
                    return {"content": content[:1000]}
                except:
                    return {"content": "", "error": "Clipboard error"}

            elif endpoint == "/api/clipboard" and method == "POST":
                content = data.get("content", "") if data else ""
                pyperclip.copy(content)
                return {"status": "copied"}

            elif endpoint == "/api/rustdesk/status" and method == "GET":
                return get_rustdesk_status()

            elif endpoint == "/api/system/info" and method == "GET":
                return get_system_info()

            elif endpoint == "/api/action/lock" and method == "POST":
                ctypes.windll.user32.LockWorkStation()
                return {"status": "locked"}

            elif endpoint == "/api/action/sleep" and method == "POST":
                subprocess.run(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"],
                    check=True
                )
                return {"status": "sleeping"}

            elif endpoint == "/api/action/screenshot" and method == "POST":
                try:
                    import pyautogui
                    screenshot_path = Path.home() / "Pictures" / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                    screenshot = pyautogui.screenshot()
                    screenshot.save(str(screenshot_path))
                    return {"status": "captured", "path": str(screenshot_path)}
                except:
                    return {"status": "error", "error": "Screenshot failed"}

            elif endpoint == "/api/health" and method == "GET":
                return {"status": "ok", "timestamp": datetime.now().isoformat(), "platform": "windows"}

            # Window info endpoint
            elif endpoint.startswith("/api/windows/") and "/info" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_CAPTURE:
                    return WindowCapture.get_window_info(int(window_id))
                return {"error": "Window capture not available"}

            # Window snapshot endpoint
            elif endpoint.startswith("/api/windows/") and "/snapshot" in endpoint:
                window_id = endpoint.split("/")[3]
                quality = data.get("quality", 60) if data else 60
                max_width = data.get("max_width", 800) if data else 800
                if HAS_CAPTURE:
                    result = WindowCapture.capture_window(int(window_id), quality=quality, max_width=max_width)
                    if result:
                        b64_data, width, height = result
                        return {"frame": b64_data, "width": width, "height": height, "window_id": window_id}
                    return {"error": "Window not available"}
                return {"error": "Window capture not available"}

            # Chrome control endpoints
            elif endpoint.startswith("/api/windows/") and "/chrome/" in endpoint:
                parts = endpoint.split("/")
                window_id = parts[3]
                action = parts[5]  # navigate, back, forward, refresh, etc.
                hwnd = int(window_id)

                if not HAS_CAPTURE or ChromeController is None:
                    return {"error": "Chrome control not available"}

                if action == "navigate":
                    url = data.get("url", "") if data else ""
                    success = ChromeController.navigate_to_url(hwnd, url)
                elif action == "back":
                    success = ChromeController.go_back(hwnd)
                elif action == "forward":
                    success = ChromeController.go_forward(hwnd)
                elif action == "refresh":
                    success = ChromeController.refresh(hwnd)
                elif action == "new-tab":
                    success = ChromeController.new_tab(hwnd)
                elif action == "close-tab":
                    success = ChromeController.close_tab(hwnd)
                elif action == "next-tab":
                    success = ChromeController.next_tab(hwnd)
                elif action == "prev-tab":
                    success = ChromeController.prev_tab(hwnd)
                else:
                    return {"error": f"Unknown chrome action: {action}"}

                return {"status": "success" if success else "failed"}

            else:
                return {"error": f"Unknown endpoint: {endpoint}"}

        except Exception as e:
            return {"error": str(e)}

    async def start_stream(self, window_id: str, options: dict):
        """Start streaming a window."""
        if not HAS_CAPTURE:
            await self.send_stream_error(window_id, "Window capture not available")
            return

        # Stop existing stream if any
        await self.stop_stream(window_id)

        fps = options.get("fps", 8)
        quality = options.get("quality", 60)
        max_width = options.get("max_width", 800)

        self.stream_options[window_id] = {"fps": fps, "quality": quality, "max_width": max_width}
        self.active_streams[window_id] = asyncio.create_task(
            self._capture_loop(window_id, fps, quality, max_width)
        )

        print(f"Started stream for window {window_id} at {fps} FPS")

    async def stop_stream(self, window_id: str):
        """Stop streaming a window."""
        if window_id in self.active_streams:
            self.active_streams[window_id].cancel()
            try:
                await self.active_streams[window_id]
            except asyncio.CancelledError:
                pass
            del self.active_streams[window_id]
            if window_id in self.stream_options:
                del self.stream_options[window_id]
            print(f"Stopped stream for window {window_id}")

    async def _capture_loop(self, window_id: str, fps: int, quality: int, max_width: int):
        """Capture and stream frames."""
        interval = 1.0 / fps
        hwnd = int(window_id)
        seq = 0

        while True:
            try:
                result = WindowCapture.capture_window(hwnd, quality=quality, max_width=max_width)

                if result is None:
                    await self.send_stream_error(window_id, "Window not available")
                    break

                b64_data, width, height = result
                seq += 1

                # Send frame to relay
                if self.ws:
                    await self.ws.send_json({
                        "type": "stream_frame",
                        "window_id": window_id,
                        "frame": b64_data,
                        "width": width,
                        "height": height,
                        "seq": seq
                    })

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Capture error: {e}")
                await self.send_stream_error(window_id, str(e))
                break

    async def send_stream_error(self, window_id: str, error: str):
        """Send stream error to relay."""
        if self.ws:
            try:
                await self.ws.send_json({
                    "type": "stream_error",
                    "window_id": window_id,
                    "error": error
                })
            except:
                pass

    async def stop_all_streams(self):
        """Stop all active streams."""
        for window_id in list(self.active_streams.keys()):
            await self.stop_stream(window_id)

    async def connect(self):
        """Connect to the relay server."""
        ws_url = self.relay_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/ws/pc?token={self.auth_token}"

        print(f"Connecting to relay: {ws_url.replace(self.auth_token, '***')}")

        async with aiohttp.ClientSession() as session:
            while self.running:
                try:
                    async with session.ws_connect(ws_url, heartbeat=30) as ws:
                        self.ws = ws
                        print("Connected to relay server!")

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                msg_type = data.get("type", "")

                                if msg_type == "request":
                                    # Handle API request
                                    request_id = data.get("request_id")
                                    endpoint = data.get("endpoint")
                                    method = data.get("method", "GET")
                                    req_data = data.get("data")

                                    print(f"Request: {method} {endpoint}")

                                    result = await self.handle_request(
                                        request_id, endpoint, method, req_data
                                    )

                                    # Send response
                                    await ws.send_json({
                                        "type": "response",
                                        "request_id": request_id,
                                        "data": result
                                    })

                                elif msg_type == "stream_start":
                                    # Start streaming a window
                                    window_id = data.get("window_id")
                                    options = data.get("options", {})
                                    if window_id:
                                        await self.start_stream(window_id, options)

                                elif msg_type == "stream_stop":
                                    # Stop streaming a window
                                    window_id = data.get("window_id")
                                    if window_id:
                                        await self.stop_stream(window_id)

                                elif msg_type == "stream_adjust":
                                    # Adjust stream settings
                                    window_id = data.get("window_id")
                                    options = data.get("options", {})
                                    if window_id:
                                        await self.stop_stream(window_id)
                                        await self.start_stream(window_id, options)

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"WebSocket error: {ws.exception()}")
                                break

                except aiohttp.ClientError as e:
                    print(f"Connection error: {e}")
                except Exception as e:
                    print(f"Error: {e}")

                # Stop all streams on disconnect
                await self.stop_all_streams()

                if self.running:
                    print("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def run(self):
        """Run the relay client."""
        self.running = True
        print("=" * 50)
        print("RustDesk Mobile UI - Relay Client")
        print("=" * 50)
        print(f"Relay URL: {self.relay_url}")
        print(f"pywin32: {'OK' if HAS_WIN32 else 'MISSING'}")
        print(f"pycaw: {'OK' if HAS_PYCAW else 'MISSING'}")
        print(f"screen_brightness_control: {'OK' if HAS_SBC else 'MISSING'}")
        print(f"Window Capture: {'OK' if HAS_CAPTURE else 'MISSING'}")
        print("=" * 50)
        print("Press Ctrl+C to stop")
        print()

        try:
            await self.connect()
        except KeyboardInterrupt:
            print("\nStopping...")
            self.running = False


def main():
    parser = argparse.ArgumentParser(description="RustDesk Mobile UI Relay Client")
    parser.add_argument("relay_url", help="URL of the relay server (e.g., https://your-app.onrender.com)")
    parser.add_argument("--token", "-t", default="Devops$@2026",
                        help="Authentication token (must match relay server)")
    args = parser.parse_args()

    client = RelayClient(args.relay_url, args.token)
    asyncio.run(client.run())


if __name__ == "__main__":
    main()
