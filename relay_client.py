#!/usr/bin/env python3
"""
RustDesk Mobile UI - Relay Client (Runs on your PC)
Connects to the relay server and executes commands locally.
Supports live window streaming and interactive terminal sessions.
"""

import asyncio
import json
import os
import sys
import argparse
import threading
import queue
from urllib.parse import urljoin
from typing import Dict, Set, Optional

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


class TerminalSession:
    """Manages an interactive terminal session."""

    def __init__(self, session_id: str, shell: str = None):
        self.session_id = session_id
        self.shell = shell or ("powershell.exe" if os.name == 'nt' else "/bin/bash")
        self.process: Optional[subprocess.Popen] = None
        self.output_queue: queue.Queue = queue.Queue()
        self.running = False
        self.history: list = []
        self.cwd = os.path.expanduser("~")

    def start(self):
        """Start the terminal session."""
        if self.running:
            return

        self.running = True

        # For Windows, we'll use a simpler approach - execute commands one at a time
        # and return output, rather than a persistent shell (which is complex on Windows)
        self.output_queue.put({
            "type": "output",
            "text": f"Terminal session started\nWorking directory: {self.cwd}\n\n$ ",
            "cwd": self.cwd
        })

    def execute(self, command: str) -> None:
        """Execute a command and queue the output."""
        if not self.running:
            return

        # Add to history
        self.history.append(command)

        # Handle built-in commands
        if command.strip().lower() == "clear" or command.strip().lower() == "cls":
            self.output_queue.put({
                "type": "clear"
            })
            self.output_queue.put({
                "type": "output",
                "text": "$ ",
                "cwd": self.cwd
            })
            return

        if command.strip().lower().startswith("cd "):
            new_dir = command.strip()[3:].strip()
            try:
                # Handle ~ expansion
                if new_dir.startswith("~"):
                    new_dir = os.path.expanduser(new_dir)
                # Handle relative paths
                if not os.path.isabs(new_dir):
                    new_dir = os.path.join(self.cwd, new_dir)
                new_dir = os.path.normpath(new_dir)

                if os.path.isdir(new_dir):
                    self.cwd = new_dir
                    self.output_queue.put({
                        "type": "output",
                        "text": f"\n$ ",
                        "cwd": self.cwd
                    })
                else:
                    self.output_queue.put({
                        "type": "output",
                        "text": f"\ncd: no such directory: {new_dir}\n$ ",
                        "cwd": self.cwd
                    })
            except Exception as e:
                self.output_queue.put({
                    "type": "output",
                    "text": f"\ncd error: {e}\n$ ",
                    "cwd": self.cwd
                })
            return

        # Execute command in subprocess
        try:
            if os.name == 'nt':
                # Windows: use cmd /c for better compatibility
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=self.cwd,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}
                )
            else:
                # Unix
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=self.cwd
                )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += result.stderr
            if not output:
                output = ""

            self.output_queue.put({
                "type": "output",
                "text": f"\n{output}\n$ " if output else "\n$ ",
                "cwd": self.cwd,
                "exit_code": result.returncode
            })

        except subprocess.TimeoutExpired:
            self.output_queue.put({
                "type": "output",
                "text": "\n[Command timed out after 30 seconds]\n$ ",
                "cwd": self.cwd,
                "exit_code": -1
            })
        except Exception as e:
            self.output_queue.put({
                "type": "output",
                "text": f"\n[Error: {e}]\n$ ",
                "cwd": self.cwd,
                "exit_code": -1
            })

    def get_output(self) -> Optional[dict]:
        """Get pending output from the queue."""
        try:
            return self.output_queue.get_nowait()
        except queue.Empty:
            return None

    def get_history(self, index: int = -1) -> Optional[str]:
        """Get command from history."""
        if not self.history:
            return None
        try:
            return self.history[index]
        except IndexError:
            return None

    def stop(self):
        """Stop the terminal session."""
        self.running = False
        if self.process:
            try:
                self.process.terminate()
            except:
                pass


class RelayClient:
    def __init__(self, relay_url: str, auth_token: str):
        self.relay_url = relay_url.rstrip('/')
        self.auth_token = auth_token
        self.ws = None
        self.running = False
        self.active_streams: Dict[str, asyncio.Task] = {}  # window_id -> capture task
        self.stream_options: Dict[str, dict] = {}  # window_id -> {fps, quality, max_width}
        self.terminal_sessions: Dict[str, TerminalSession] = {}  # session_id -> TerminalSession

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

    # ========== TERMINAL SESSION METHODS ==========

    async def start_terminal(self, session_id: str):
        """Start a new terminal session."""
        # Stop existing session if any
        if session_id in self.terminal_sessions:
            await self.stop_terminal(session_id)

        session = TerminalSession(session_id)
        session.start()
        self.terminal_sessions[session_id] = session

        print(f"Started terminal session: {session_id}")

        # Send initial output
        await self._flush_terminal_output(session_id)

    async def terminal_execute(self, session_id: str, command: str):
        """Execute a command in a terminal session."""
        session = self.terminal_sessions.get(session_id)
        if not session:
            await self.send_terminal_output(session_id, {
                "type": "error",
                "text": "Terminal session not found. Starting new session...\n"
            })
            await self.start_terminal(session_id)
            session = self.terminal_sessions.get(session_id)

        if session:
            # Echo the command
            await self.send_terminal_output(session_id, {
                "type": "input_echo",
                "text": command + "\n"
            })

            # Execute in thread to not block
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, session.execute, command)

            # Send output
            await self._flush_terminal_output(session_id)

    async def _flush_terminal_output(self, session_id: str):
        """Flush all pending output from a terminal session."""
        session = self.terminal_sessions.get(session_id)
        if not session:
            return

        while True:
            output = session.get_output()
            if output is None:
                break
            await self.send_terminal_output(session_id, output)

    async def send_terminal_output(self, session_id: str, output: dict):
        """Send terminal output to the relay."""
        if self.ws:
            try:
                await self.ws.send_json({
                    "type": "terminal_output",
                    "session_id": session_id,
                    **output
                })
            except Exception as e:
                print(f"Error sending terminal output: {e}")

    async def stop_terminal(self, session_id: str):
        """Stop a terminal session."""
        session = self.terminal_sessions.pop(session_id, None)
        if session:
            session.stop()
            print(f"Stopped terminal session: {session_id}")

    async def stop_all_terminals(self):
        """Stop all terminal sessions."""
        for session_id in list(self.terminal_sessions.keys()):
            await self.stop_terminal(session_id)

    # ========== KEYSTROKE FORWARDING ==========

    async def send_keystroke_to_window(self, window_id: str, key: str, modifiers: dict):
        """Send a keystroke to an actual window using PostMessage."""
        if not HAS_WIN32:
            print("[KEYSTROKE] pywin32 not available")
            return

        try:
            import win32api
            import time

            # Parse window ID (hex string to int)
            if window_id.startswith("0x"):
                hwnd = int(window_id, 16)
            else:
                hwnd = int(window_id)

            # Focus the window first
            try:
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.05)
            except Exception as e:
                print(f"[KEYSTROKE] Could not focus window: {e}")

            # Get modifier states
            ctrl = modifiers.get("ctrl", False)
            alt = modifiers.get("alt", False)
            shift = modifiers.get("shift", False)

            # Convert key to virtual key code
            vk_code = self._key_to_vk(key)
            if vk_code is None:
                print(f"[KEYSTROKE] Unknown key: {key}")
                return

            # Press modifiers
            if ctrl:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_CONTROL, 0)
            if alt:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_MENU, 0)
            if shift:
                win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_SHIFT, 0)

            time.sleep(0.01)

            # Send key press and release
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk_code, 0)
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk_code, 0)

            time.sleep(0.01)

            # Release modifiers
            if shift:
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_SHIFT, 0)
            if alt:
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_MENU, 0)
            if ctrl:
                win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_CONTROL, 0)

            print(f"[KEYSTROKE] Sent '{key}' to window {window_id}")

        except Exception as e:
            print(f"[KEYSTROKE] Error: {e}")

    def _key_to_vk(self, key: str) -> int:
        """Convert a key name to Windows virtual key code."""
        # Special keys mapping
        special_keys = {
            "Enter": 0x0D,
            "Tab": 0x09,
            "Escape": 0x1B,
            "Backspace": 0x08,
            "Delete": 0x2E,
            "ArrowUp": 0x26,
            "ArrowDown": 0x28,
            "ArrowLeft": 0x25,
            "ArrowRight": 0x27,
            "Home": 0x24,
            "End": 0x23,
            "PageUp": 0x21,
            "PageDown": 0x22,
            "Space": 0x20,
            " ": 0x20,
            "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
            "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
            "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
        }

        if key in special_keys:
            return special_keys[key]

        # Single character - use its ASCII/Unicode value
        if len(key) == 1:
            return ord(key.upper())

        return None

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

                                # Terminal session handlers
                                elif msg_type == "terminal_start":
                                    session_id = data.get("session_id", "default")
                                    print(f"[TERMINAL] Starting session: {session_id}")
                                    await self.start_terminal(session_id)

                                elif msg_type == "terminal_input":
                                    session_id = data.get("session_id", "default")
                                    print(f"[TERMINAL] Input for {session_id}: {data.get('command', '')}")
                                    command = data.get("command", "")
                                    await self.terminal_execute(session_id, command)

                                elif msg_type == "terminal_stop":
                                    session_id = data.get("session_id", "default")
                                    await self.stop_terminal(session_id)

                                elif msg_type == "terminal_keystroke":
                                    # Send keystroke to actual terminal window
                                    window_id = data.get("window_id")
                                    key = data.get("key", "")
                                    modifiers = data.get("modifiers", {})
                                    if window_id and key:
                                        await self.send_keystroke_to_window(window_id, key, modifiers)

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"WebSocket error: {ws.exception()}")
                                break

                except aiohttp.ClientError as e:
                    print(f"Connection error: {e}")
                except Exception as e:
                    print(f"Error: {e}")

                # Stop all streams and terminals on disconnect
                await self.stop_all_streams()
                await self.stop_all_terminals()

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
        print(f"Terminal Sessions: OK")
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
