#!/usr/bin/env python3
"""
RustDesk Mobile UI - Relay Client (Runs on your PC)
Connects to the relay server and executes commands locally.
"""

import asyncio
import json
import os
import sys
import argparse
from urllib.parse import urljoin

import aiohttp

# Import the local server functions
from server import (
    load_app_config, get_window_list, get_volume, set_volume,
    get_brightness, set_brightness, get_system_info, get_rustdesk_status,
    HAS_WIN32, HAS_PYCAW, HAS_SBC
)

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

            else:
                return {"error": f"Unknown endpoint: {endpoint}"}

        except Exception as e:
            return {"error": str(e)}

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

                                if data.get("type") == "request":
                                    # Handle request
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

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"WebSocket error: {ws.exception()}")
                                break

                except aiohttp.ClientError as e:
                    print(f"Connection error: {e}")
                except Exception as e:
                    print(f"Error: {e}")

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
