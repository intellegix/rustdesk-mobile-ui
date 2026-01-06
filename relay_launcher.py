#!/usr/bin/env python3
"""
RustDesk Mobile UI - Relay Client Launcher
Simple GUI to configure and run the relay client.
"""

import os
import sys
import json
import socket
import asyncio
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

# Config file location
CONFIG_FILE = Path.home() / ".rustdesk-mobile-ui" / "config.json"

def load_config():
    """Load config from file."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {
        "relay_url": "https://rustdesk-mobile-ui.onrender.com",
        "token": "Devops$@2026",
        "host_name": socket.gethostname()
    }

def save_config(config):
    """Save config to file."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


class RelayLauncher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("RustDesk Mobile UI - Relay Client")
        self.root.geometry("450x350")
        self.root.resizable(False, False)

        # Try to set icon
        try:
            self.root.iconbitmap("icon.ico")
        except:
            pass

        # Apply dark theme
        self.style = ttk.Style()
        self.root.configure(bg='#1a1a2e')

        # Configure styles
        self.style.configure('TLabel', background='#1a1a2e', foreground='#d1d5db', font=('Segoe UI', 10))
        self.style.configure('TEntry', fieldbackground='#2d2d44', foreground='#ffffff')
        self.style.configure('TButton', font=('Segoe UI', 10))
        self.style.configure('Header.TLabel', font=('Segoe UI', 16, 'bold'), foreground='#f97316')
        self.style.configure('Status.TLabel', font=('Segoe UI', 10), foreground='#9ca3af')

        self.config = load_config()
        self.running = False
        self.client = None

        self.create_widgets()

    def create_widgets(self):
        # Main frame
        main_frame = tk.Frame(self.root, bg='#1a1a2e', padx=30, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header
        header = ttk.Label(main_frame, text="🖥️ Relay Client", style='Header.TLabel')
        header.pack(pady=(0, 20))

        # Relay URL
        url_frame = tk.Frame(main_frame, bg='#1a1a2e')
        url_frame.pack(fill=tk.X, pady=5)
        ttk.Label(url_frame, text="Relay URL:").pack(anchor=tk.W)
        self.url_entry = ttk.Entry(url_frame, width=50)
        self.url_entry.insert(0, self.config.get('relay_url', ''))
        self.url_entry.pack(fill=tk.X, pady=(5, 0))

        # Token
        token_frame = tk.Frame(main_frame, bg='#1a1a2e')
        token_frame.pack(fill=tk.X, pady=10)
        ttk.Label(token_frame, text="Password:").pack(anchor=tk.W)
        self.token_entry = ttk.Entry(token_frame, width=50, show="*")
        self.token_entry.insert(0, self.config.get('token', ''))
        self.token_entry.pack(fill=tk.X, pady=(5, 0))

        # Host Name
        name_frame = tk.Frame(main_frame, bg='#1a1a2e')
        name_frame.pack(fill=tk.X, pady=5)
        ttk.Label(name_frame, text="Host Name (display name):").pack(anchor=tk.W)
        self.name_entry = ttk.Entry(name_frame, width=50)
        self.name_entry.insert(0, self.config.get('host_name', socket.gethostname()))
        self.name_entry.pack(fill=tk.X, pady=(5, 0))

        # Status
        self.status_label = ttk.Label(main_frame, text="Status: Not Connected", style='Status.TLabel')
        self.status_label.pack(pady=15)

        # Buttons
        btn_frame = tk.Frame(main_frame, bg='#1a1a2e')
        btn_frame.pack(fill=tk.X, pady=10)

        self.connect_btn = tk.Button(
            btn_frame,
            text="Connect",
            command=self.toggle_connection,
            bg='#f97316',
            fg='white',
            font=('Segoe UI', 11, 'bold'),
            padx=30,
            pady=8,
            relief=tk.FLAT,
            cursor='hand2'
        )
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.save_btn = tk.Button(
            btn_frame,
            text="Save Settings",
            command=self.save_settings,
            bg='#2d2d44',
            fg='white',
            font=('Segoe UI', 10),
            padx=20,
            pady=8,
            relief=tk.FLAT,
            cursor='hand2'
        )
        self.save_btn.pack(side=tk.LEFT, padx=5)

        # Footer
        footer = ttk.Label(main_frame, text="Run this on any PC you want to control remotely", style='Status.TLabel')
        footer.pack(side=tk.BOTTOM, pady=10)

    def save_settings(self):
        self.config = {
            "relay_url": self.url_entry.get().strip(),
            "token": self.token_entry.get(),
            "host_name": self.name_entry.get().strip()
        }
        save_config(self.config)
        messagebox.showinfo("Saved", "Settings saved successfully!")

    def toggle_connection(self):
        if self.running:
            self.stop_client()
        else:
            self.start_client()

    def start_client(self):
        self.save_settings()

        relay_url = self.url_entry.get().strip()
        token = self.token_entry.get()
        host_name = self.name_entry.get().strip()

        if not relay_url:
            messagebox.showerror("Error", "Please enter a relay URL")
            return

        self.running = True
        self.connect_btn.config(text="Disconnect", bg='#ef4444')
        self.status_label.config(text="Status: Connecting...")

        # Disable inputs
        self.url_entry.config(state='disabled')
        self.token_entry.config(state='disabled')
        self.name_entry.config(state='disabled')

        # Start client in background thread
        def run_client():
            try:
                # Import here to avoid circular imports
                from relay_client import RelayClient

                self.client = RelayClient(relay_url, token, host_name=host_name)

                # Update status when connected
                self.root.after(0, lambda: self.status_label.config(text="Status: Connected ✓"))

                asyncio.run(self.client.run())
            except Exception as e:
                self.root.after(0, lambda: self.on_client_error(str(e)))
            finally:
                self.root.after(0, self.on_client_stopped)

        self.client_thread = threading.Thread(target=run_client, daemon=True)
        self.client_thread.start()

    def stop_client(self):
        if self.client:
            self.client.running = False
        self.running = False

    def on_client_error(self, error):
        self.status_label.config(text=f"Status: Error - {error[:50]}")
        messagebox.showerror("Connection Error", error)

    def on_client_stopped(self):
        self.running = False
        self.connect_btn.config(text="Connect", bg='#f97316')
        self.status_label.config(text="Status: Disconnected")

        # Re-enable inputs
        self.url_entry.config(state='normal')
        self.token_entry.config(state='normal')
        self.name_entry.config(state='normal')

    def run(self):
        self.root.mainloop()


def main():
    app = RelayLauncher()
    app.run()


if __name__ == "__main__":
    main()
