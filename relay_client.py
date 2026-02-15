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
import subprocess
import threading
import queue
import time
from concurrent.futures import ThreadPoolExecutor
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
import time

# Import pyautogui for reliable keyboard input
try:
    import pyautogui
    pyautogui.FAILSAFE = False  # Disable failsafe for headless operation
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False
    print("[WARNING] pyautogui not installed - terminal commands won't work")

# Try Windows imports
try:
    import win32gui
    import win32con
    import win32api
except ImportError:
    pass


class NonBlockingTerminalManager:
    """Manages non-blocking terminal execution to prevent WebSocket event loop blocking."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="terminal")
        self.active_sessions = {}

    async def execute_command_async(self, session: 'TerminalSession', command: str) -> dict:
        """Execute terminal command without blocking main event loop"""
        loop = asyncio.get_event_loop()

        try:
            # Run terminal command in thread pool to avoid blocking
            result = await loop.run_in_executor(
                self.executor,
                self._execute_command_sync,
                session,
                command
            )
            return result
        except Exception as e:
            return {
                "type": "output",
                "text": f"\n[Error executing command]: {e}\n$ ",
                "cwd": session.cwd,
                "exit_code": -1
            }

    def _execute_command_sync(self, session: 'TerminalSession', command: str) -> dict:
        """Synchronous command execution (runs in thread pool)"""
        try:
            if os.name == 'nt':
                # Windows: use cmd /c for better compatibility
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=session.cwd,
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
                    cwd=session.cwd
                )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += result.stderr
            if not output:
                output = ""

            return {
                "type": "output",
                "text": f"\n{output}\n$ " if output else "\n$ ",
                "cwd": session.cwd,
                "exit_code": result.returncode
            }

        except subprocess.TimeoutExpired:
            return {
                "type": "output",
                "text": "\n[Command timed out after 30 seconds]\n$ ",
                "cwd": session.cwd,
                "exit_code": -1
            }
        except Exception as e:
            return {
                "type": "output",
                "text": f"\n[Command error]: {e}\n$ ",
                "cwd": session.cwd,
                "exit_code": -1
            }

    def shutdown(self):
        """Shutdown the thread pool executor"""
        self.executor.shutdown(wait=True)

class SmartReconnectionManager:
    """Manages intelligent reconnection with exponential backoff and jitter."""

    def __init__(self):
        self.base_delay = 2.0
        self.max_delay = 60.0
        self.consecutive_failures = 0
        self.successful_connections = 0

    def get_reconnect_delay(self) -> float:
        """Calculate smart reconnection delay with exponential backoff and jitter"""
        if self.consecutive_failures == 0:
            return self.base_delay

        # Exponential backoff with 1.5x multiplier (gentler than 2x)
        delay = min(
            self.base_delay * (1.5 ** self.consecutive_failures),
            self.max_delay
        )

        # Add random jitter (±20%) to prevent thundering herd
        import random
        jitter = delay * 0.2 * (random.random() - 0.5)
        final_delay = max(1.0, delay + jitter)

        return final_delay

    def on_connection_success(self):
        """Reset backoff on successful connection"""
        self.consecutive_failures = 0
        self.successful_connections += 1
        print(f"[RECONNECT] Connection successful (total: {self.successful_connections})")

    def on_connection_failure(self, error: str):
        """Increment failure count and log attempt"""
        self.consecutive_failures += 1
        delay = self.get_reconnect_delay()
        print(f"[RECONNECT] Connection failed (attempt #{self.consecutive_failures}): {error}")
        print(f"[RECONNECT] Retrying in {delay:.1f} seconds...")
        return delay

# Global non-blocking terminal manager
terminal_manager = NonBlockingTerminalManager()

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

    async def execute(self, command: str) -> None:
        """Execute a command and queue the output (non-blocking)."""
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

        # Execute command using non-blocking terminal manager
        try:
            # Use the global terminal manager for non-blocking execution
            result = await terminal_manager.execute_command_async(self, command)
            self.output_queue.put(result)
        except Exception as e:
            self.output_queue.put({
                "type": "output",
                "text": f"\n[Async Error: {e}]\n$ ",
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
    def __init__(self, relay_url: str, auth_token: str, host_id: str = None, host_name: str = None):
        self.relay_url = relay_url.rstrip('/')
        self.auth_token = auth_token
        self.ws = None
        self.running = False
        self.active_streams: Dict[str, asyncio.Task] = {}  # window_id -> capture task
        self.stream_options: Dict[str, dict] = {}  # window_id -> {fps, quality, max_width}
        self.reconnection_manager = SmartReconnectionManager()
        self.terminal_sessions: Dict[str, TerminalSession] = {}  # session_id -> TerminalSession

        # Host identification for multi-host support
        import socket
        import platform
        self.host_id = host_id or socket.gethostname().lower().replace(' ', '-')
        self.host_name = host_name or socket.gethostname()
        self.platform = platform.system()
        self.platform_version = platform.release()

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

            elif endpoint.startswith("/api/windows/") and "/restore" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                return {"status": "restored"}

            elif endpoint.startswith("/api/windows/") and "/snap/left" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    screen_width = win32api.GetSystemMetrics(0)
                    screen_height = win32api.GetSystemMetrics(1)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(hwnd, None, 0, 0, screen_width // 2, screen_height, 0)
                return {"status": "snapped_left"}

            elif endpoint.startswith("/api/windows/") and "/snap/right" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    screen_width = win32api.GetSystemMetrics(0)
                    screen_height = win32api.GetSystemMetrics(1)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(hwnd, None, screen_width // 2, 0, screen_width // 2, screen_height, 0)
                return {"status": "snapped_right"}

            elif endpoint.startswith("/api/windows/") and "/snap/top-left" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    screen_width = win32api.GetSystemMetrics(0)
                    screen_height = win32api.GetSystemMetrics(1)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(hwnd, None, 0, 0, screen_width // 2, screen_height // 2, 0)
                return {"status": "snapped_top_left"}

            elif endpoint.startswith("/api/windows/") and "/snap/top-right" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    screen_width = win32api.GetSystemMetrics(0)
                    screen_height = win32api.GetSystemMetrics(1)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(hwnd, None, screen_width // 2, 0, screen_width // 2, screen_height // 2, 0)
                return {"status": "snapped_top_right"}

            elif endpoint.startswith("/api/windows/") and "/snap/bottom-left" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    screen_width = win32api.GetSystemMetrics(0)
                    screen_height = win32api.GetSystemMetrics(1)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(hwnd, None, 0, screen_height // 2, screen_width // 2, screen_height // 2, 0)
                return {"status": "snapped_bottom_left"}

            elif endpoint.startswith("/api/windows/") and "/snap/bottom-right" in endpoint:
                window_id = endpoint.split("/")[3]
                if HAS_WIN32:
                    hwnd = int(window_id)
                    screen_width = win32api.GetSystemMetrics(0)
                    screen_height = win32api.GetSystemMetrics(1)
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetWindowPos(hwnd, None, screen_width // 2, screen_height // 2, screen_width // 2, screen_height // 2, 0)
                return {"status": "snapped_bottom_right"}

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

            elif endpoint == "/api/paste-image" and method == "POST":
                # Paste image to clipboard and simulate Ctrl+V
                try:
                    import base64
                    import io
                    from PIL import Image
                    import win32clipboard

                    image_b64 = data.get("image", "") if data else ""
                    window_id = data.get("window_id", "") if data else ""

                    if not image_b64:
                        return {"error": "No image data"}

                    # Decode base64 to image
                    image_data = base64.b64decode(image_b64)
                    image = Image.open(io.BytesIO(image_data))

                    # Convert to BMP format for clipboard
                    output = io.BytesIO()
                    image.convert("RGB").save(output, "BMP")
                    bmp_data = output.getvalue()[14:]  # Strip BMP header
                    output.close()

                    # Copy to clipboard
                    win32clipboard.OpenClipboard()
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp_data)
                    win32clipboard.CloseClipboard()

                    # Focus the target window before pasting
                    if window_id and HAS_WIN32:
                        try:
                            hwnd = int(window_id)
                            if win32gui.IsIconic(hwnd):
                                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                            win32gui.SetForegroundWindow(hwnd)
                            time.sleep(0.15)  # Give window time to focus
                        except Exception as e:
                            print(f"[PASTE-IMAGE] Could not focus window: {e}")

                    # Simulate paste - try multiple methods for different terminals
                    time.sleep(0.1)

                    # For Windows Terminal / Claude Code, right-click is most reliable for images
                    pyautogui.click(button='right')
                    time.sleep(0.2)

                    # Also try keyboard shortcuts as fallback
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.1)
                    pyautogui.hotkey('ctrl', 'shift', 'v')

                    print(f"[PASTE-IMAGE] Image pasted to window {window_id}")
                    return {"status": "pasted"}
                except Exception as e:
                    return {"error": str(e)}

            elif endpoint == "/api/rustdesk/status" and method == "GET":
                return get_rustdesk_status()

            elif endpoint == "/api/rustdesk/connect" and method == "POST":
                # Connect to a RustDesk device by ID
                device_id = data.get("device_id") if data else None
                if not device_id:
                    return {"error": "device_id required"}
                try:
                    # Find RustDesk executable
                    rustdesk_paths = [
                        r"C:\Program Files\RustDesk\rustdesk.exe",
                        r"C:\Program Files (x86)\RustDesk\rustdesk.exe",
                        os.path.expanduser(r"~\AppData\Local\RustDesk\rustdesk.exe"),
                    ]
                    rustdesk_exe = None
                    for path in rustdesk_paths:
                        if os.path.exists(path):
                            rustdesk_exe = path
                            break

                    if not rustdesk_exe:
                        return {"error": "RustDesk not found"}

                    # Launch RustDesk with connection
                    subprocess.Popen([rustdesk_exe, "--connect", str(device_id)])
                    print(f"[RUSTDESK] Connecting to device: {device_id}")
                    return {"status": "connecting", "device_id": device_id}
                except Exception as e:
                    return {"error": str(e)}

            elif endpoint == "/api/rustdesk/devices" and method == "GET":
                # Return saved RustDesk devices
                return {
                    "devices": [
                        {"id": "415005013", "name": "Network Device", "description": "Available on current network"}
                    ]
                }

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

            # Folders API endpoints
            elif endpoint == "/api/folders/search" and method == "POST":
                query = data.get("query", "") if data else ""
                if not query:
                    return {"error": "No query provided"}
                result = await self.search_folder_with_claude(query)
                return result

            elif endpoint == "/api/folders/open" and method == "POST":
                path = data.get("path", "") if data else ""
                if not path:
                    return {"error": "No path provided"}
                result = self.open_folder(path)
                return result

            else:
                return {"error": f"Unknown endpoint: {endpoint}"}

        except Exception as e:
            return {"error": str(e)}

    async def search_folder_with_claude(self, query: str) -> dict:
        """Use Claude API to find a folder based on user description."""
        try:
            import anthropic

            # Get API key from environment
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return {"error": "ANTHROPIC_API_KEY not set in environment"}

            # Get common folder locations to help Claude
            home = os.path.expanduser("~")
            common_paths = [
                f"User home: {home}",
                f"Desktop: {os.path.join(home, 'Desktop')}",
                f"Documents: {os.path.join(home, 'Documents')}",
                f"Downloads: {os.path.join(home, 'Downloads')}",
                f"Pictures: {os.path.join(home, 'Pictures')}",
                f"Videos: {os.path.join(home, 'Videos')}",
                f"Music: {os.path.join(home, 'Music')}",
            ]

            # Add Dropbox path if it exists
            dropbox_path = os.path.join(home, "ASR Dropbox")
            if os.path.exists(dropbox_path):
                common_paths.append(f"Dropbox: {dropbox_path}")

            # List top-level folders in common locations
            folder_listing = []
            for path in [home, os.path.join(home, "Documents"), os.path.join(home, "Desktop"), dropbox_path]:
                if os.path.exists(path):
                    try:
                        folders = [f for f in os.listdir(path) if os.path.isdir(os.path.join(path, f)) and not f.startswith('.')]
                        folder_listing.append(f"\nFolders in {path}:\n" + "\n".join(f"  - {f}" for f in folders[:20]))
                    except PermissionError:
                        pass

            context = "\n".join(common_paths) + "\n" + "\n".join(folder_listing)

            client = anthropic.Anthropic(api_key=api_key)
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": f"""Based on the user's description, determine the most likely folder path on this Windows PC.

Common locations:
{context}

User wants to open: "{query}"

Respond with ONLY the full folder path that best matches their description. No explanation, just the path.
If you can't determine a specific path, respond with the most logical location based on their description.
Always use backslashes for Windows paths."""
                    }
                ]
            )

            # Extract the path from Claude's response
            path = message.content[0].text.strip()

            # Clean up the path (remove quotes if present)
            path = path.strip('"\'')

            # Verify the path exists
            if os.path.exists(path) and os.path.isdir(path):
                return {"path": path}
            else:
                # Try to find a close match
                if os.path.exists(os.path.dirname(path)):
                    return {"path": path, "note": "Path may not exist yet"}
                return {"error": f"Folder not found: {path}"}

        except ImportError:
            return {"error": "anthropic library not installed. Run: pip install anthropic"}
        except Exception as e:
            return {"error": str(e)}

    def open_folder(self, path: str) -> dict:
        """Open a folder in Windows Explorer."""
        try:
            # Normalize the path
            path = os.path.normpath(path)

            if not os.path.exists(path):
                return {"error": f"Path does not exist: {path}"}

            if not os.path.isdir(path):
                # If it's a file, open its parent directory
                path = os.path.dirname(path)

            # Open in Explorer
            subprocess.Popen(['explorer', path])
            return {"status": "success", "path": path}
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
        """Capture and stream frames (non-blocking)."""
        interval = 1.0 / fps
        hwnd = int(window_id)
        seq = 0

        while True:
            try:
                # Use thread pool for non-blocking window capture
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,  # Default executor
                    WindowCapture.capture_window,
                    hwnd, quality, max_width
                )

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

    async def handle_health_ping(self, data: dict):
        """Respond to health ping from relay server"""
        ping_id = data.get("ping_id")
        server_timestamp = data.get("timestamp", 0)
        current_time = time.time()

        if self.ws:
            try:
                await self.ws.send_json({
                    "type": "health_pong",
                    "ping_id": ping_id,
                    "server_timestamp": server_timestamp,
                    "client_timestamp": current_time,
                    "latency": (current_time - server_timestamp) * 1000
                })
                print(f"[HEALTH] Responded to ping {ping_id}")
            except Exception as e:
                print(f"[HEALTH] Failed to respond to ping: {e}")

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

            # Execute using the new non-blocking method
            await session.execute(command)

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

    def _reliable_focus(self, hwnd: int, max_retries: int = 3) -> bool:
        """Reliably focus a window with AttachThreadInput and retry loop."""
        try:
            # Restore if minimized
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.1)

            # Use AttachThreadInput for reliable focusing
            current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            target_thread = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
            attached = False

            if current_thread != target_thread:
                attached = ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, True)

            try:
                for attempt in range(max_retries):
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.1)
                    if win32gui.GetForegroundWindow() == hwnd:
                        print(f"[FOCUS] Window {hwnd} focused on attempt {attempt + 1}")
                        return True
                    time.sleep(0.1)

                print(f"[FOCUS] Warning: Could not verify focus after {max_retries} attempts")
                return False
            finally:
                if attached:
                    ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, False)

        except Exception as e:
            print(f"[FOCUS] Error focusing window: {e}")
            return False

    async def send_terminal_command(self, window_id: str, command: str):
        """Type a command into the terminal window using pyautogui."""
        if not HAS_PYAUTOGUI:
            print("[COMMAND] pyautogui not available")
            return

        if not HAS_WIN32:
            print("[COMMAND] pywin32 not available")
            return

        try:
            # Parse window ID
            if window_id.startswith("0x"):
                hwnd = int(window_id, 16)
            else:
                hwnd = int(window_id)

            print(f"[COMMAND] Focusing window {hwnd}...")

            # Reliable focus with retry and thread attachment
            self._reliable_focus(hwnd)
            time.sleep(0.25)  # Increased post-focus delay for stability

            # Type the command using pyautogui (more reliable than PostMessage)
            print(f"[COMMAND] Typing: {command}")
            pyautogui.typewrite(command, interval=0.02)

            # Press Enter to execute
            time.sleep(0.05)
            pyautogui.press('enter')

            print(f"[COMMAND] Sent command to window {hwnd}: {command}")

        except Exception as e:
            print(f"[COMMAND] Error: {e}")

    async def send_terminal_key(self, window_id: str, key: str, modifiers: dict):
        """Send a special key (Ctrl+C, arrows, etc) to terminal using pyautogui."""
        if not HAS_PYAUTOGUI:
            print("[KEY] pyautogui not available")
            return

        if not HAS_WIN32:
            print("[KEY] pywin32 not available")
            return

        try:
            # Parse window ID
            if window_id.startswith("0x"):
                hwnd = int(window_id, 16)
            else:
                hwnd = int(window_id)

            print(f"[KEY] Focusing window {hwnd}...")

            # Reliable focus with retry and thread attachment
            self._reliable_focus(hwnd)
            time.sleep(0.15)

            # Map key names to pyautogui key names
            key_map = {
                "ArrowUp": "up",
                "ArrowDown": "down",
                "ArrowLeft": "left",
                "ArrowRight": "right",
                "Enter": "enter",
                "Tab": "tab",
                "Escape": "escape",
                "Backspace": "backspace",
                "Delete": "delete",
                "Home": "home",
                "End": "end",
                "PageUp": "pageup",
                "PageDown": "pagedown",
            }

            pyautogui_key = key_map.get(key, key.lower())

            # Build modifier list
            mods = []
            if modifiers.get("ctrl"):
                mods.append("ctrl")
            if modifiers.get("alt"):
                mods.append("alt")
            if modifiers.get("shift"):
                mods.append("shift")

            print(f"[KEY] Sending key: {pyautogui_key} with modifiers: {mods}")

            # Send key with modifiers using hotkey
            if mods:
                pyautogui.hotkey(*mods, pyautogui_key)
            else:
                pyautogui.press(pyautogui_key)

            print(f"[KEY] Sent key {key} to window {hwnd}")

        except Exception as e:
            print(f"[KEY] Error: {e}")

    async def connect(self):
        """Connect to the relay server."""
        ws_url = self.relay_url.replace("https://", "wss://").replace("http://", "ws://")
        # Include host_id in the URL for multi-host support
        ws_url = f"{ws_url}/ws/pc?token={self.auth_token}&host_id={self.host_id}"

        print(f"Connecting to relay: {ws_url.replace(self.auth_token, '***')}")
        print(f"Host ID: {self.host_id}")
        print(f"Host Name: {self.host_name}")

        async with aiohttp.ClientSession() as session:
            while self.running:
                delay = 5  # Default delay in case of unexpected control flow
                try:
                    async with session.ws_connect(ws_url, heartbeat=30) as ws:
                        self.ws = ws
                        print("Connected to relay server!")

                        # Reset reconnection backoff on successful connection
                        self.reconnection_manager.on_connection_success()

                        # Send host registration
                        await ws.send_json({
                            "type": "host_register",
                            "host_id": self.host_id,
                            "host_name": self.host_name,
                            "platform": self.platform,
                            "platform_version": self.platform_version,
                            "capabilities": {
                                "window_capture": HAS_CAPTURE,
                                "volume_control": HAS_PYCAW,
                                "brightness_control": HAS_SBC,
                                "keyboard_control": HAS_PYAUTOGUI
                            }
                        })

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

                                elif msg_type == "health_ping":
                                    # Respond to health check ping from relay server
                                    await self.handle_health_ping(data)

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
                                    # Send keystroke to actual terminal window (legacy)
                                    window_id = data.get("window_id")
                                    key = data.get("key", "")
                                    modifiers = data.get("modifiers", {})
                                    print(f"[KEYSTROKE] Received: window={window_id}, key={key}, mods={modifiers}")
                                    if window_id and key:
                                        await self.send_keystroke_to_window(window_id, key, modifiers)
                                    else:
                                        print(f"[KEYSTROKE] Missing window_id or key")

                                elif msg_type == "terminal_command":
                                    # Type full command to terminal window using pyautogui
                                    window_id = data.get("window_id")
                                    command = data.get("command", "")
                                    print(f"[COMMAND] Received: window={window_id}, command={command}")
                                    if window_id and command:
                                        await self.send_terminal_command(window_id, command)
                                    else:
                                        print(f"[COMMAND] Missing window_id or command")

                                elif msg_type == "terminal_key":
                                    # Send special key to terminal using pyautogui
                                    window_id = data.get("window_id")
                                    key = data.get("key", "")
                                    modifiers = data.get("modifiers", {})
                                    print(f"[KEY] Received: window={window_id}, key={key}, mods={modifiers}")
                                    if window_id and key:
                                        await self.send_terminal_key(window_id, key, modifiers)
                                    else:
                                        print(f"[KEY] Missing window_id or key")

                                elif msg_type == "remote_click":
                                    # Click at position in browser window
                                    window_id = data.get("window_id")
                                    x = data.get("x", 0)
                                    y = data.get("y", 0)
                                    print(f"[CLICK] Received: window={window_id}, x={x}, y={y}")
                                    if window_id and HAS_WIN32:
                                        try:
                                            hwnd = int(window_id)
                                            # Get window position
                                            rect = win32gui.GetWindowRect(hwnd)
                                            # Calculate absolute position
                                            abs_x = rect[0] + x
                                            abs_y = rect[1] + y
                                            # Focus window first
                                            if win32gui.IsIconic(hwnd):
                                                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                            win32gui.SetForegroundWindow(hwnd)
                                            await asyncio.sleep(0.1)
                                            # Click at position
                                            pyautogui.click(abs_x, abs_y)
                                            print(f"[CLICK] Clicked at ({abs_x}, {abs_y})")
                                        except Exception as e:
                                            print(f"[CLICK] Error: {e}")

                                elif msg_type == "remote_scroll":
                                    # Scroll in browser window
                                    window_id = data.get("window_id")
                                    delta_y = data.get("delta_y", 0)
                                    print(f"[SCROLL] Received: window={window_id}, delta_y={delta_y}")
                                    if window_id and HAS_WIN32:
                                        try:
                                            hwnd = int(window_id)
                                            # Focus window
                                            if win32gui.IsIconic(hwnd):
                                                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                            win32gui.SetForegroundWindow(hwnd)
                                            await asyncio.sleep(0.05)
                                            # Convert delta to scroll clicks (negative = scroll down)
                                            scroll_clicks = int(delta_y / 30)
                                            if scroll_clicks != 0:
                                                pyautogui.scroll(-scroll_clicks)
                                                print(f"[SCROLL] Scrolled {-scroll_clicks} clicks")
                                        except Exception as e:
                                            print(f"[SCROLL] Error: {e}")

                                elif msg_type == "remote_mouse":
                                    # Mouse operations for text selection (long press to select)
                                    window_id = data.get("window_id")
                                    action = data.get("action")  # 'down', 'move', 'up'
                                    x = data.get("x", 0)
                                    y = data.get("y", 0)
                                    print(f"[MOUSE] {action} at ({x}, {y}) on window {window_id}")
                                    if window_id and HAS_WIN32 and HAS_PYAUTOGUI:
                                        try:
                                            hwnd = int(window_id)
                                            # Get window position
                                            rect = win32gui.GetWindowRect(hwnd)
                                            abs_x = rect[0] + x
                                            abs_y = rect[1] + y

                                            # Focus window first
                                            if win32gui.IsIconic(hwnd):
                                                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                            try:
                                                win32gui.SetForegroundWindow(hwnd)
                                            except:
                                                pass
                                            await asyncio.sleep(0.02)

                                            # Execute mouse action
                                            if action == "down":
                                                pyautogui.moveTo(abs_x, abs_y)
                                                pyautogui.mouseDown()
                                                print(f"[MOUSE] Mouse down at ({abs_x}, {abs_y})")
                                            elif action == "move":
                                                pyautogui.moveTo(abs_x, abs_y)
                                            elif action == "up":
                                                pyautogui.moveTo(abs_x, abs_y)
                                                pyautogui.mouseUp()
                                                print(f"[MOUSE] Mouse up at ({abs_x}, {abs_y})")
                                        except Exception as e:
                                            print(f"[MOUSE] Error: {e}")

                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"WebSocket error: {ws.exception()}")
                                break

                except aiohttp.ClientError as e:
                    # Connection successful initially, then failed
                    delay = self.reconnection_manager.on_connection_failure(f"Connection error: {e}")
                except Exception as e:
                    # Connection failed with general error
                    delay = self.reconnection_manager.on_connection_failure(f"General error: {e}")
                else:
                    # Connection closed normally (not through exception)
                    delay = self.reconnection_manager.on_connection_failure("Connection closed")

                # Stop all streams and terminals on disconnect
                await self.stop_all_streams()
                await self.stop_all_terminals()

                if self.running:
                    await asyncio.sleep(delay)

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
        print(f"pyautogui: {'OK' if HAS_PYAUTOGUI else 'MISSING'}")
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
    parser.add_argument("--host-id", "-i", default=None,
                        help="Custom host ID (default: computer name)")
    parser.add_argument("--host-name", "-n", default=None,
                        help="Display name for this host (default: computer name)")
    args = parser.parse_args()

    client = RelayClient(args.relay_url, args.token, args.host_id, args.host_name)
    asyncio.run(client.run())


if __name__ == "__main__":
    main()
