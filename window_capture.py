#!/usr/bin/env python3
"""
RustDesk Mobile UI - Window Capture Module
Captures individual window content using Win32 APIs.
"""

import io
import base64
from typing import Optional, Tuple, Dict, Any
from ctypes import windll, byref, c_int, sizeof, Structure, c_void_p
from ctypes.wintypes import DWORD, HWND, RECT

# Try importing Windows-specific libraries
try:
    import win32gui
    import win32ui
    import win32con
    import win32process
    import win32api
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Process name classifications
TERMINAL_PROCESSES = {
    'windowsterminal.exe', 'cmd.exe', 'powershell.exe', 'pwsh.exe',
    'conhost.exe', 'mintty.exe', 'alacritty.exe', 'hyper.exe',
    'wt.exe', 'windows terminal'
}

BROWSER_PROCESSES = {
    'chrome.exe': 'chrome',
    'msedge.exe': 'edge',
    'firefox.exe': 'firefox',
    'brave.exe': 'brave',
    'opera.exe': 'opera',
    'vivaldi.exe': 'vivaldi'
}


class WindowCapture:
    """Handles window capture using Win32 APIs."""

    @staticmethod
    def is_available() -> bool:
        """Check if window capture is available."""
        return HAS_WIN32 and HAS_PIL

    @staticmethod
    def get_window_info(hwnd: int) -> Dict[str, Any]:
        """Get detailed information about a window."""
        if not HAS_WIN32:
            return {"error": "pywin32 not available"}

        try:
            # Check if window exists
            if not win32gui.IsWindow(hwnd):
                return {"error": "Window does not exist"}

            # Get window rectangle
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]

            # Get window title
            title = win32gui.GetWindowText(hwnd)

            # Get window class
            window_class = win32gui.GetClassName(hwnd)

            # Get process info
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = WindowCapture._get_process_name(pid)

            # Classify window type
            window_type = WindowCapture.classify_window(hwnd, process_name)

            # Check window state
            is_minimized = win32gui.IsIconic(hwnd)
            is_maximized = WindowCapture._is_maximized(hwnd)
            is_visible = win32gui.IsWindowVisible(hwnd)

            return {
                "hwnd": hwnd,
                "title": title,
                "class": window_class,
                "process": process_name,
                "pid": pid,
                "type": window_type,
                "bounds": {
                    "x": rect[0],
                    "y": rect[1],
                    "width": width,
                    "height": height
                },
                "state": {
                    "minimized": is_minimized,
                    "maximized": is_maximized,
                    "visible": is_visible
                }
            }
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def classify_window(hwnd: int, process_name: str = None) -> str:
        """
        Classify a window by type.
        Returns: 'terminal', 'browser', or 'generic'
        """
        if not HAS_WIN32:
            return "generic"

        try:
            if process_name is None:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                process_name = WindowCapture._get_process_name(pid)

            process_lower = process_name.lower()

            # Check for terminal
            for term_process in TERMINAL_PROCESSES:
                if term_process in process_lower:
                    return "terminal"

            # Check for browser
            for browser_exe in BROWSER_PROCESSES.keys():
                if browser_exe in process_lower:
                    return "browser"

            # Check window class for additional detection
            try:
                window_class = win32gui.GetClassName(hwnd).lower()
                if 'consolewindowclass' in window_class:
                    return "terminal"
                if 'chrome_widgetwin' in window_class or 'mozillawindowclass' in window_class:
                    return "browser"
            except:
                pass

            return "generic"
        except:
            return "generic"

    @staticmethod
    def capture_window(hwnd: int, quality: int = 60, max_width: int = 800) -> Optional[Tuple[str, int, int]]:
        """
        Capture a window and return base64-encoded JPEG.

        Args:
            hwnd: Window handle
            quality: JPEG quality (1-100)
            max_width: Maximum width for output image (for mobile optimization)

        Returns:
            Tuple of (base64_data, width, height) or None if capture failed
        """
        if not HAS_WIN32 or not HAS_PIL:
            return None

        try:
            # Check if window exists
            if not win32gui.IsWindow(hwnd):
                return None

            # If window is minimized, restore it temporarily for capture
            was_minimized = win32gui.IsIconic(hwnd)
            if was_minimized:
                # Can't capture minimized windows reliably
                return None

            # Get window dimensions
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]

            if width <= 0 or height <= 0:
                return None

            # Create device contexts
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()

            # Create bitmap
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(bitmap)

            # Use PrintWindow for better capture (works with layered windows)
            # PW_RENDERFULLCONTENT = 2 for better capture on Win 8.1+
            result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

            if result == 0:
                # Fallback to BitBlt
                save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)

            # Convert to PIL Image
            bmpinfo = bitmap.GetInfo()
            bmpstr = bitmap.GetBitmapBits(True)

            img = Image.frombuffer(
                'RGB',
                (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                bmpstr, 'raw', 'BGRX', 0, 1
            )

            # Cleanup Win32 resources
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

            # Resize for mobile if needed
            if width > max_width:
                ratio = max_width / width
                new_height = int(height * ratio)
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                width, height = max_width, new_height

            # Convert to JPEG
            buffer = io.BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            buffer.seek(0)

            # Encode to base64
            b64_data = base64.b64encode(buffer.getvalue()).decode('utf-8')

            return (b64_data, width, height)

        except Exception as e:
            print(f"Capture error: {e}")
            return None

    @staticmethod
    def _get_process_name(pid: int) -> str:
        """Get process name from PID."""
        try:
            import psutil
            process = psutil.Process(pid)
            return process.name()
        except:
            pass

        # Fallback using win32
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                False, pid
            )
            try:
                exe_path = win32process.GetModuleFileNameEx(handle, 0)
                return exe_path.split('\\')[-1]
            finally:
                win32api.CloseHandle(handle)
        except:
            return "unknown"

    @staticmethod
    def _is_maximized(hwnd: int) -> bool:
        """Check if window is maximized."""
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            return placement[1] == win32con.SW_SHOWMAXIMIZED
        except:
            return False


class ChromeController:
    """Controls Chrome browser windows via keyboard automation."""

    @staticmethod
    def is_available() -> bool:
        """Check if Chrome control is available."""
        return HAS_WIN32

    @staticmethod
    def navigate_to_url(hwnd: int, url: str) -> bool:
        """
        Navigate Chrome to a URL.
        Uses Ctrl+L to focus address bar, then types URL and presses Enter.
        """
        if not HAS_WIN32:
            return False

        try:
            # Focus the window first
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)

            import time
            time.sleep(0.1)  # Brief delay for focus

            # Send Ctrl+L to focus address bar
            _send_key_combo(hwnd, 'l', ctrl=True)
            time.sleep(0.05)

            # Clear any existing text
            _send_key_combo(hwnd, 'a', ctrl=True)
            time.sleep(0.02)

            # Type the URL using clipboard (faster than SendMessage)
            import pyperclip
            old_clipboard = pyperclip.paste()
            pyperclip.copy(url)
            _send_key_combo(hwnd, 'v', ctrl=True)
            time.sleep(0.02)
            pyperclip.copy(old_clipboard)  # Restore clipboard

            # Press Enter
            _send_key(hwnd, win32con.VK_RETURN)

            return True
        except Exception as e:
            print(f"Navigate error: {e}")
            return False

    @staticmethod
    def go_back(hwnd: int) -> bool:
        """Go back in browser history (Alt+Left)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key_combo(hwnd, win32con.VK_LEFT, alt=True)
            return True
        except:
            return False

    @staticmethod
    def go_forward(hwnd: int) -> bool:
        """Go forward in browser history (Alt+Right)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key_combo(hwnd, win32con.VK_RIGHT, alt=True)
            return True
        except:
            return False

    @staticmethod
    def refresh(hwnd: int) -> bool:
        """Refresh the page (F5)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key(hwnd, win32con.VK_F5)
            return True
        except:
            return False

    @staticmethod
    def new_tab(hwnd: int) -> bool:
        """Open new tab (Ctrl+T)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key_combo(hwnd, 't', ctrl=True)
            return True
        except:
            return False

    @staticmethod
    def close_tab(hwnd: int) -> bool:
        """Close current tab (Ctrl+W)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key_combo(hwnd, 'w', ctrl=True)
            return True
        except:
            return False

    @staticmethod
    def next_tab(hwnd: int) -> bool:
        """Switch to next tab (Ctrl+Tab)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key_combo(hwnd, win32con.VK_TAB, ctrl=True)
            return True
        except:
            return False

    @staticmethod
    def prev_tab(hwnd: int) -> bool:
        """Switch to previous tab (Ctrl+Shift+Tab)."""
        if not HAS_WIN32:
            return False
        try:
            win32gui.SetForegroundWindow(hwnd)
            _send_key_combo(hwnd, win32con.VK_TAB, ctrl=True, shift=True)
            return True
        except:
            return False


def _send_key(hwnd: int, vk_code: int):
    """Send a single key press."""
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk_code, 0)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk_code, 0)


def _send_key_combo(hwnd: int, key, ctrl: bool = False, alt: bool = False, shift: bool = False):
    """Send a key combination."""
    import time

    # Convert character to virtual key code if needed
    if isinstance(key, str):
        vk_code = ord(key.upper())
    else:
        vk_code = key

    # Press modifiers
    if ctrl:
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_CONTROL, 0)
    if alt:
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_MENU, 0)
    if shift:
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_SHIFT, 0)

    time.sleep(0.01)

    # Press main key
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


# Test code
if __name__ == "__main__":
    print(f"Win32 available: {HAS_WIN32}")
    print(f"PIL available: {HAS_PIL}")
    print(f"Window capture available: {WindowCapture.is_available()}")

    if HAS_WIN32:
        # List some windows
        def enum_callback(hwnd, results):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    results.append((hwnd, title))

        windows = []
        win32gui.EnumWindows(enum_callback, windows)

        print("\nVisible windows:")
        for hwnd, title in windows[:10]:
            info = WindowCapture.get_window_info(hwnd)
            print(f"  {hwnd}: {title[:40]} [{info.get('type', 'unknown')}]")
