#!/usr/bin/env python3
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, date, time as datetime_time
import ctypes
import math
import platform
import tkinter as tk

# Work around a broken `platform.win32_ver()` under some Python distributions
# (e.g. Anaconda on Windows), where the underlying `ver` subprocess output
# fails to decode as cp1252. darkdetect (a customtkinter dependency) calls
# this at import time to check for Windows dark mode; a hard failure here
# would crash the whole app before the UI even starts, so fall back to the
# OS build number directly via the registry instead of shelling out.
try:
    platform.win32_ver()
except Exception:
    _real_win32_ver = platform.win32_ver

    def _safe_win32_ver(*args, **kwargs):
        try:
            return _real_win32_ver(*args, **kwargs)
        except Exception:
            return ("10", "", "", "")

    platform.win32_ver = _safe_win32_ver

import customtkinter as ctk
import schedule
from plyer import notification
import pystray
from PIL import Image

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

COLOR_BG = "#12181f"
COLOR_CARD = "#1b2430"
COLOR_ACCENT = "#3aa9ff"
COLOR_GOOD = "#31d68a"
COLOR_STREAK = "#ff9f43"
COLOR_MUTED = "#7c8a9a"
COLOR_TEXT = "#eef2f6"
COLOR_RING_TRACK = "#26313f"

sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None


def _app_dir():
    """
    Where user data (config, hydration db) lives. When frozen by PyInstaller,
    sys.executable is the .exe itself, so its folder is used — NOT
    sys._MEIPASS, which is a temp extraction dir that's wiped between runs
    and would silently reset all settings/history every launch.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundled_asset_path(filename):
    """
    Where read-only shipped assets (icons) live. Under PyInstaller these are
    unpacked into sys._MEIPASS; in a normal script run it's just the script's
    own directory.
    """
    base = getattr(sys, "_MEIPASS", _app_dir())
    return os.path.join(base, filename)


CONFIG_FILE = os.path.join(_app_dir(), "config.json")
HYDRATION_DB_FILE = os.path.join(_app_dir(), "hydration.db")
ICON_FILE = _bundled_asset_path("drop.png")
ICON_ICO_FILE = _bundled_asset_path("drop.ico")

DEFAULT_CONFIG = {
    "interval_minutes": 90,
    "daily_goal": 8,
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "focus_keywords": ["meeting", "zoom", "vs code", "terminal"],
    "escalation_enabled": True,
    "reminder_levels": ["gentle", "normal", "urgent"]
}

DEFAULT_HYDRATION = {
    "today": {"drinks": 0, "goal": 8, "last_drink": None},
    "streak": 0,
    "last_streak_date": None,
    "history": []
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    else:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def _get_db():
    conn = sqlite3.connect(HYDRATION_DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drink_log (
            date TEXT PRIMARY KEY,
            drinks INTEGER NOT NULL,
            goal INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def _migrate_legacy_json_if_present():
    legacy_file = os.path.join(_app_dir(), "hydration.json")
    if not os.path.exists(legacy_file):
        return None
    try:
        with open(legacy_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def load_hydration():
    conn = _get_db()
    try:
        rows = dict(conn.execute("SELECT key, value FROM state").fetchall())
        if not rows:
            legacy = _migrate_legacy_json_if_present()
            if legacy is not None:
                print("[Migration] Importing existing hydration.json into hydration.db")
                save_hydration(legacy)
                return legacy
            save_hydration(DEFAULT_HYDRATION)
            return json.loads(json.dumps(DEFAULT_HYDRATION))

        history = [
            {"date": d, "drinks": drinks, "goal": goal}
            for d, drinks, goal in conn.execute(
                "SELECT date, drinks, goal FROM drink_log ORDER BY date"
            ).fetchall()
        ]

        return {
            "today": {
                "drinks": int(rows.get("today_drinks", 0)),
                "goal": int(rows.get("today_goal", DEFAULT_CONFIG["daily_goal"])),
                "last_drink": rows.get("today_last_drink") or None,
            },
            "streak": int(rows.get("streak", 0)),
            "last_streak_date": rows.get("last_streak_date") or None,
            "current_date": rows.get("current_date") or None,
            "missed_count": int(rows.get("missed_count", 0)),
            "history": history,
        }
    finally:
        conn.close()


def save_hydration(hydration):
    conn = _get_db()
    try:
        state = {
            "today_drinks": hydration["today"]["drinks"],
            "today_goal": hydration["today"]["goal"],
            "today_last_drink": hydration["today"].get("last_drink") or "",
            "streak": hydration["streak"],
            "last_streak_date": hydration.get("last_streak_date") or "",
            "current_date": hydration.get("current_date") or "",
            "missed_count": hydration.get("missed_count", 0),
        }
        conn.executemany(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in state.items()]
        )
        conn.executemany(
            "INSERT INTO drink_log (date, drinks, goal) VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET drinks = excluded.drinks, goal = excluded.goal",
            [(entry["date"], entry["drinks"], entry["goal"]) for entry in hydration.get("history", []) if entry.get("date")]
        )
        conn.commit()
    finally:
        conn.close()


def is_quiet_hours(config):
    now = datetime.now().time()
    start_str = config["quiet_hours"]["start"]
    end_str = config["quiet_hours"]["end"]
    start = datetime.strptime(start_str, "%H:%M").time()
    end = datetime.strptime(end_str, "%H:%M").time()

    if start < end:
        return start <= now < end
    else:
        return now >= start or now < end


def get_active_window_title():
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLength(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def is_focus_active(config):
    title = get_active_window_title().lower()
    keywords = [kw.lower() for kw in config["focus_keywords"]]
    return any(kw in title for kw in keywords)


def log_drink(hydration, config):
    now = datetime.now()
    hydration["today"]["drinks"] += 1
    hydration["today"]["last_drink"] = now.strftime("%H:%M")

    if hydration["today"]["drinks"] >= hydration["today"]["goal"]:
        today_str = date.today().isoformat()
        last_streak_date = hydration.get("last_streak_date")
        yesterday_str = (date.today() - __import__('datetime').timedelta(days=1)).isoformat()

        if last_streak_date is None or last_streak_date == yesterday_str:
            hydration["streak"] += 1
        else:
            hydration["streak"] = 1

        hydration["last_streak_date"] = today_str

    hydration["missed_count"] = 0
    save_hydration(hydration)
    print(f"💧 Logged drink at {hydration['today']['last_drink']} ({hydration['today']['drinks']}/{hydration['today']['goal']})")


def send_notification(level, hydration, config):
    levels = {
        "gentle": ("💧 Sip of water?", f"Gentle reminder (1st). You've had {hydration['today']['drinks']}/{hydration['today']['goal']} drinks today.", 5),
        "normal": ("💧 Time to hydrate!", f"You're at {hydration['today']['drinks']}/{hydration['today']['goal']} drinks today.", 10),
        "urgent": ("🚨 You've been ignoring reminders!", f"Drink water now! ({hydration['today']['drinks']}/{hydration['today']['goal']} today)", 15)
    }

    title, message, timeout = levels.get(level, levels["gentle"])
    notification.notify(
        title=title,
        message=message,
        timeout=timeout,
        app_name="Remind me to Drink"
    )
    print(f"[{level.upper()}] {title}")


def check_daily_rollover(hydration):
    today_str = date.today().isoformat()
    if "current_date" not in hydration or hydration["current_date"] != today_str:
        if hydration["today"]["drinks"] > 0:
            hydration["history"].append({
                "date": hydration.get("current_date", today_str),
                "drinks": hydration["today"]["drinks"],
                "goal": hydration["today"]["goal"]
            })

        hydration["today"] = {"drinks": 0, "goal": hydration["today"]["goal"], "last_drink": None}
        hydration["current_date"] = today_str
        hydration["missed_count"] = 0
        save_hydration(hydration)
        print(f"📅 Daily rollover — streak: {hydration['streak']} days")


def reminder_tick(config, hydration, tray_icon):
    check_daily_rollover(hydration)

    if is_quiet_hours(config):
        print("🤫 In quiet hours, skipping reminder")
        return

    if is_focus_active(config):
        print("🔍 Focus mode active, skipping reminder")
        return

    if not config.get("escalation_enabled", True):
        hydration["missed_count"] = 0
    else:
        hydration["missed_count"] = hydration.get("missed_count", 0) + 1

    level_idx = min(hydration["missed_count"] - 1, 2)
    level = config["reminder_levels"][level_idx]

    send_notification(level, hydration, config)
    save_hydration(hydration)


def make_tray_icon(config, hydration):
    def on_drink(icon, item):
        log_drink(hydration, config)
        update_tray_status(icon, hydration)

    def on_status(icon, item):
        show_status_window(hydration, config)

    def on_settings(icon, item):
        show_settings_window(config)

    menu_items = [
        pystray.MenuItem("I drank water 💧", on_drink),
        pystray.MenuItem("Show status", on_status),
        pystray.MenuItem("Settings", on_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: quit_app(icon))
    ]

    menu = pystray.Menu(*menu_items)

    try:
        icon = Image.open(ICON_FILE)
    except FileNotFoundError:
        print(f"[Icon] '{ICON_FILE}' not found. Using placeholder.")
        icon = Image.new("RGB", (64, 64), color="cyan")

    return pystray.Icon("RemindMeToDrink", icon, title="Remind me to Drink", menu=menu)


def update_tray_status(icon, hydration):
    drinks = hydration["today"]["drinks"]
    goal = hydration["today"]["goal"]
    icon.title = f"Water: {drinks}/{goal} | Streak: {hydration['streak']} days"


def _apply_window_icon(window):
    """
    Set both the title-bar/taskbar icon (needs a real .ico on Windows —
    iconbitmap() silently fails on .png) and the window's iconphoto as a
    fallback for platforms/themes where iconbitmap doesn't stick.
    """
    if os.path.exists(ICON_ICO_FILE):
        try:
            window.iconbitmap(ICON_ICO_FILE)
        except Exception:
            pass
    if os.path.exists(ICON_FILE):
        try:
            photo = tk.PhotoImage(file=ICON_FILE)
            window.iconphoto(True, photo)
            window._icon_photo_ref = photo
        except Exception:
            pass


def _draw_progress_ring(canvas, size, thickness, fraction, ring_color):
    canvas.delete("all")
    pad = thickness / 2 + 2
    canvas.create_oval(pad, pad, size - pad, size - pad, outline=COLOR_RING_TRACK, width=thickness)

    fraction = max(0.0, min(1.0, fraction))
    if fraction > 0:
        extent = -359.999 if fraction >= 0.9995 else -360 * fraction
        canvas.create_arc(
            pad, pad, size - pad, size - pad,
            start=90, extent=extent,
            style="arc", outline=ring_color, width=thickness
        )

        # Place the % label right at the tip of the arc. tkinter angles are
        # measured counterclockwise from 3 o'clock, and the arc sweeps
        # clockwise from the top (90 deg) by `extent` degrees, so the tip
        # sits at angle (90 + extent).
        cx = cy = size / 2
        radius = (size - pad * 2) / 2
        tip_deg = math.radians(90 + extent)
        tip_x = cx + radius * math.cos(tip_deg)
        tip_y = cy - radius * math.sin(tip_deg)

        pct = round(fraction * 100)
        label_bg_r = 13
        canvas.create_oval(
            tip_x - label_bg_r, tip_y - label_bg_r, tip_x + label_bg_r, tip_y + label_bg_r,
            fill=COLOR_BG, outline=ring_color, width=2
        )
        canvas.create_text(
            tip_x, tip_y, text=str(pct),
            fill=ring_color, font=("Segoe UI", 9, "bold")
        )


def _last_n_days_history(hydration, n=7):
    history = {entry.get("date"): entry for entry in hydration.get("history", []) if entry.get("date")}
    today = date.today()
    days = []
    for i in range(n - 1, -1, -1):
        d = today - __import__("datetime").timedelta(days=i)
        d_str = d.isoformat()
        if d_str == today.isoformat():
            drinks = hydration["today"]["drinks"]
            goal = hydration["today"]["goal"]
        else:
            entry = history.get(d_str)
            drinks = entry["drinks"] if entry else 0
            goal = entry["goal"] if entry else hydration["today"]["goal"]
        days.append((d, drinks, goal))
    return days


class UIThread:
    """
    Owns a single hidden Tk root plus its mainloop on one dedicated thread.
    tkinter (and by extension customtkinter) is not thread-safe: every
    widget must be created and touched from the thread that runs the
    mainloop. Opening a fresh Tk()/CTk() instance per window on its own
    throwaway thread (the earlier approach) breaks as soon as a second
    window is opened, since each thread's Tcl interpreter fights over
    process-global state. Routing all window creation through .submit()
    onto this one thread's queue avoids that.
    """

    def __init__(self):
        self._root = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self):
        self._root = ctk.CTk()
        self._root.withdraw()
        self._ready.set()
        self._root.mainloop()

    def submit(self, fn):
        if self._root is not None:
            self._root.after(0, fn)


_ui_thread = None
_ui_thread_lock = threading.Lock()


def get_ui_thread():
    global _ui_thread
    with _ui_thread_lock:
        if _ui_thread is None:
            _ui_thread = UIThread()
    return _ui_thread


class StatusWindow(ctk.CTkToplevel):
    RING_SIZE = 200
    RING_THICKNESS = 14

    def __init__(self, master, hydration, config):
        super().__init__(master)
        self.hydration = hydration
        self.config_data = config

        self.title("Hydration")
        self.geometry("380x560")
        self.resizable(False, False)
        self.configure(fg_color=COLOR_BG)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.lift()
        self.focus_force()

        _apply_window_icon(self)

        self._build_layout()
        self._refresh()
        self.after(50, self._refresh)

    def _build_layout(self):
        header = ctk.CTkLabel(
            self, text="Hydration", font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=COLOR_TEXT
        )
        header.pack(pady=(24, 4))

        subheader = ctk.CTkLabel(
            self, text="Today's progress", font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COLOR_MUTED
        )
        subheader.pack(pady=(0, 16))

        ring_card = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=20)
        ring_card.pack(padx=24, pady=(0, 16), fill="x")

        self.ring_canvas = tk.Canvas(
            ring_card, width=self.RING_SIZE, height=self.RING_SIZE,
            bg=COLOR_CARD, highlightthickness=0
        )
        self.ring_canvas.pack(pady=20)

        self.ring_text = self.ring_canvas.create_text(
            self.RING_SIZE / 2, self.RING_SIZE / 2 - 10,
            text="", fill=COLOR_TEXT, font=("Segoe UI", 26, "bold")
        )
        self.ring_subtext = self.ring_canvas.create_text(
            self.RING_SIZE / 2, self.RING_SIZE / 2 + 20,
            text="drinks", fill=COLOR_MUTED, font=("Segoe UI", 11)
        )

        self.cups_canvas = tk.Canvas(
            ring_card, height=28, bg=COLOR_CARD, highlightthickness=0
        )
        self.cups_canvas.pack(pady=(0, 8), padx=16, fill="x")

        self.last_drink_label = ctk.CTkLabel(
            ring_card, text="", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLOR_MUTED
        )
        self.last_drink_label.pack(pady=(0, 16))

        stats_row = ctk.CTkFrame(self, fg_color="transparent")
        stats_row.pack(padx=24, pady=(0, 16), fill="x")
        stats_row.grid_columnconfigure((0, 1), weight=1)

        self.streak_card = ctk.CTkFrame(stats_row, fg_color=COLOR_CARD, corner_radius=16)
        self.streak_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.streak_value = ctk.CTkLabel(
            self.streak_card, text="", font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=COLOR_STREAK
        )
        self.streak_value.pack(pady=(14, 0))
        ctk.CTkLabel(
            self.streak_card, text="day streak", font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLOR_MUTED
        ).pack(pady=(0, 14))

        self.goal_card = ctk.CTkFrame(stats_row, fg_color=COLOR_CARD, corner_radius=16)
        self.goal_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.goal_value = ctk.CTkLabel(
            self.goal_card, text="", font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=COLOR_GOOD
        )
        self.goal_value.pack(pady=(14, 0))
        ctk.CTkLabel(
            self.goal_card, text="daily goal", font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COLOR_MUTED
        ).pack(pady=(0, 14))

        history_card = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=16)
        history_card.pack(padx=24, pady=(0, 16), fill="x")

        ctk.CTkLabel(
            history_card, text="Last 7 days", font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=COLOR_TEXT
        ).pack(anchor="w", padx=16, pady=(12, 8))

        self.history_bars_frame = ctk.CTkFrame(history_card, fg_color="transparent")
        self.history_bars_frame.pack(padx=16, pady=(0, 16), fill="x")

        action_row = ctk.CTkFrame(self, fg_color="transparent")
        action_row.pack(padx=24, pady=(0, 10), fill="x")
        action_row.grid_columnconfigure(0, weight=1)

        self.drink_button = ctk.CTkButton(
            action_row, text="I drank water", font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=COLOR_ACCENT, hover_color="#2f8fdb", corner_radius=14, height=44,
            command=self._on_drink
        )
        self.drink_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.undo_button = ctk.CTkButton(
            action_row, text="-1", font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color="transparent", hover_color=COLOR_CARD, text_color=COLOR_MUTED,
            border_width=1, border_color=COLOR_RING_TRACK, corner_radius=14, height=44, width=44,
            command=self._on_undo
        )
        self.undo_button.grid(row=0, column=1, sticky="ew")

        close_button = ctk.CTkButton(
            self, text="Close", font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="transparent", hover_color=COLOR_CARD, text_color=COLOR_MUTED,
            corner_radius=14, height=32, command=self.destroy
        )
        close_button.pack(padx=24, pady=(0, 20), fill="x")

    def _on_drink(self):
        log_drink(self.hydration, self.config_data)
        self._refresh()

    def _on_undo(self):
        if self.hydration["today"]["drinks"] > 0:
            self.hydration["today"]["drinks"] -= 1
            if self.hydration["today"]["drinks"] == 0:
                self.hydration["today"]["last_drink"] = None
            save_hydration(self.hydration)
            self._refresh()

    def _draw_cups(self, drinks, goal):
        canvas = self.cups_canvas
        canvas.delete("all")
        canvas.update_idletasks()
        width = canvas.winfo_width() or 332
        n = max(goal, 1)
        spacing = min(24, (width - 16) / n)
        total_width = spacing * n
        start_x = (width - total_width) / 2 + spacing / 2

        for i in range(n):
            cx = start_x + i * spacing
            filled = i < drinks
            color = COLOR_ACCENT if filled else COLOR_RING_TRACK
            canvas.create_text(cx, 14, text="💧", font=("Segoe UI Emoji", 13), fill=color)

    def _refresh(self):
        drinks = self.hydration["today"]["drinks"]
        goal = self.hydration["today"]["goal"] or 1
        streak = self.hydration["streak"]
        last_drink = self.hydration["today"].get("last_drink") or "No drinks logged yet"

        fraction = drinks / goal
        ring_color = COLOR_GOOD if fraction >= 1 else COLOR_ACCENT
        _draw_progress_ring(self.ring_canvas, self.RING_SIZE, self.RING_THICKNESS, fraction, ring_color)
        self.ring_canvas.itemconfig(self.ring_text, text=f"{drinks}/{goal}")
        self.ring_canvas.itemconfig(self.ring_subtext, text=f"{min(int(fraction * 100), 100)}% of goal")

        self._draw_cups(drinks, self.hydration["today"]["goal"])

        self.last_drink_label.configure(
            text=last_drink if last_drink == "No drinks logged yet" else f"Last drink at {last_drink}"
        )

        self.streak_value.configure(text=f"{'🔥 ' if streak > 0 else ''}{streak}")
        self.goal_value.configure(text=str(self.hydration["today"]["goal"]))

        for widget in self.history_bars_frame.winfo_children():
            widget.destroy()

        days = _last_n_days_history(self.hydration, 7)
        max_goal = max((g for _, _, g in days), default=1) or 1
        for i, (d, drinks_d, goal_d) in enumerate(days):
            col = ctk.CTkFrame(self.history_bars_frame, fg_color="transparent")
            col.grid(row=0, column=i, padx=4, sticky="s")
            self.history_bars_frame.grid_columnconfigure(i, weight=1)

            bar_h = int(50 * min(drinks_d / max_goal, 1.0)) if max_goal else 0
            bar_color = COLOR_GOOD if goal_d and drinks_d >= goal_d else COLOR_ACCENT if drinks_d > 0 else COLOR_RING_TRACK
            bar_canvas = tk.Canvas(col, width=26, height=70, bg=COLOR_CARD, highlightthickness=0)
            bar_canvas.pack()
            bar_top = 70 - bar_h
            bar_canvas.create_rectangle(5, bar_top, 21, 70, fill=bar_color, outline="")

            label_inside = bar_h >= 16
            label_y = bar_top + 10 if label_inside else bar_top - 8
            label_color = COLOR_BG if label_inside else COLOR_MUTED
            bar_canvas.create_text(
                13, label_y, text=str(drinks_d),
                fill=label_color, font=("Segoe UI", 9, "bold")
            )

            ctk.CTkLabel(
                col, text=d.strftime("%a")[0], font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=COLOR_MUTED
            ).pack(pady=(4, 0))


def show_status_window(hydration, config):
    ui = get_ui_thread()
    ui.submit(lambda: StatusWindow(ui._root, hydration, config))


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, master, config):
        super().__init__(master)
        self.config_data = config
        self.saved = False

        self.title("Settings")
        self.resizable(True, True)
        self.minsize(360, 480)
        self.configure(fg_color=COLOR_BG)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.lift()
        self.focus_force()

        self.update_idletasks()
        screen_h = self.winfo_screenheight()
        width = 400
        height = min(680, screen_h - 100)
        x = (self.winfo_screenwidth() - width) // 2
        y = max(20, (screen_h - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

        _apply_window_icon(self)

        self._build_layout()

    def _section_label(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=COLOR_MUTED
        ).pack(anchor="w", padx=24, pady=(16, 4))

    def _build_layout(self):
        header = ctk.CTkLabel(
            self, text="Settings", font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=COLOR_TEXT
        )
        header.pack(pady=(24, 4))

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", width=340, height=440)
        scroll.pack(padx=20, pady=(8, 8), fill="both", expand=True)

        # Reminder interval
        self._section_label(scroll, "Reminder interval (minutes)")
        self.interval_var = tk.IntVar(value=self.config_data["interval_minutes"])
        self.interval_value_label = ctk.CTkLabel(
            scroll, text=str(self.interval_var.get()), font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=COLOR_ACCENT
        )
        self.interval_value_label.pack(anchor="w", padx=24)
        interval_slider = ctk.CTkSlider(
            scroll, from_=15, to=180, number_of_steps=33,
            command=self._on_interval_change, progress_color=COLOR_ACCENT, button_color=COLOR_ACCENT
        )
        interval_slider.set(self.interval_var.get())
        interval_slider.pack(padx=24, pady=(4, 0), fill="x")

        # Daily goal
        self._section_label(scroll, "Daily goal (drinks)")
        self.goal_var = tk.IntVar(value=self.config_data["daily_goal"])
        self.goal_value_label = ctk.CTkLabel(
            scroll, text=str(self.goal_var.get()), font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=COLOR_GOOD
        )
        self.goal_value_label.pack(anchor="w", padx=24)
        goal_slider = ctk.CTkSlider(
            scroll, from_=1, to=16, number_of_steps=15,
            command=self._on_goal_change, progress_color=COLOR_GOOD, button_color=COLOR_GOOD
        )
        goal_slider.set(self.goal_var.get())
        goal_slider.pack(padx=24, pady=(4, 0), fill="x")

        # Quiet hours
        self._section_label(scroll, "Quiet hours")
        quiet_row = ctk.CTkFrame(scroll, fg_color="transparent")
        quiet_row.pack(padx=24, pady=(4, 0), fill="x")

        self.quiet_start_var = tk.StringVar(value=self.config_data["quiet_hours"]["start"])
        self.quiet_end_var = tk.StringVar(value=self.config_data["quiet_hours"]["end"])

        ctk.CTkLabel(quiet_row, text="From", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLOR_MUTED).grid(row=0, column=0, sticky="w")
        ctk.CTkEntry(quiet_row, textvariable=self.quiet_start_var, width=90, placeholder_text="22:00").grid(row=1, column=0, padx=(0, 12), pady=(2, 0))
        ctk.CTkLabel(quiet_row, text="To", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLOR_MUTED).grid(row=0, column=1, sticky="w")
        ctk.CTkEntry(quiet_row, textvariable=self.quiet_end_var, width=90, placeholder_text="07:00").grid(row=1, column=1, pady=(2, 0))

        # Focus keywords
        self._section_label(scroll, "Focus keywords (comma-separated)")
        self.keywords_var = tk.StringVar(value=", ".join(self.config_data["focus_keywords"]))
        ctk.CTkEntry(scroll, textvariable=self.keywords_var, width=300).pack(padx=24, pady=(4, 0), fill="x")

        # Escalation toggle
        escalation_row = ctk.CTkFrame(scroll, fg_color="transparent")
        escalation_row.pack(padx=24, pady=(20, 0), fill="x")
        self.escalation_var = tk.BooleanVar(value=self.config_data.get("escalation_enabled", True))
        ctk.CTkSwitch(
            escalation_row, text="Escalate reminders when ignored", variable=self.escalation_var,
            font=ctk.CTkFont(family="Segoe UI", size=12), progress_color=COLOR_ACCENT
        ).pack(anchor="w")

        self.status_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(family="Segoe UI", size=11), text_color=COLOR_GOOD
        )
        self.status_label.pack(pady=(4, 0))

        button_row = ctk.CTkFrame(self, fg_color="transparent")
        button_row.pack(padx=24, pady=(8, 20), fill="x")
        button_row.grid_columnconfigure((0, 1), weight=1)

        save_button = ctk.CTkButton(
            button_row, text="Save", font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=COLOR_ACCENT, hover_color="#2f8fdb", corner_radius=14, height=42,
            command=self._on_save
        )
        save_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        cancel_button = ctk.CTkButton(
            button_row, text="Cancel", font=ctk.CTkFont(family="Segoe UI", size=13),
            fg_color="transparent", hover_color=COLOR_CARD, text_color=COLOR_MUTED,
            border_width=1, border_color=COLOR_RING_TRACK, corner_radius=14, height=42,
            command=self.destroy
        )
        cancel_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _on_interval_change(self, value):
        self.interval_var.set(int(value))
        self.interval_value_label.configure(text=str(int(value)))

    def _on_goal_change(self, value):
        self.goal_var.set(int(value))
        self.goal_value_label.configure(text=str(int(value)))

    def _on_save(self):
        start = self.quiet_start_var.get().strip()
        end = self.quiet_end_var.get().strip()
        try:
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
        except ValueError:
            self.status_label.configure(text="Quiet hours must be HH:MM (e.g. 22:00)", text_color="#ff5252")
            return

        keywords = [kw.strip() for kw in self.keywords_var.get().split(",") if kw.strip()]

        self.config_data["interval_minutes"] = self.interval_var.get()
        self.config_data["daily_goal"] = self.goal_var.get()
        self.config_data["quiet_hours"]["start"] = start
        self.config_data["quiet_hours"]["end"] = end
        self.config_data["focus_keywords"] = keywords
        self.config_data["escalation_enabled"] = self.escalation_var.get()

        save_config(self.config_data)
        self.saved = True
        self.status_label.configure(text="Saved. Restart the app for the reminder interval to take effect.", text_color=COLOR_GOOD)
        self.after(1800, self.destroy)


def show_settings_window(config):
    ui = get_ui_thread()
    ui.submit(lambda: SettingsWindow(ui._root, config))


def quit_app(icon):
    print("Goodbye!")
    icon.stop()
    os._exit(0)


def run_scheduler(config, hydration, tray_icon):
    schedule.every(config["interval_minutes"]).minutes.do(reminder_tick, config, hydration, tray_icon)

    while True:
        schedule.run_pending()
        time.sleep(1)


def main():
    import sys
    test_mode = "--test" in sys.argv

    global tray_icon
    config = load_config()
    hydration = load_hydration()

    print("[Remind me to Drink] Starting...")
    print(f"[Config] Interval: {config['interval_minutes']} min | Goal: {config['daily_goal']} drinks")
    print(f"[Hydration] Today: {hydration['today']['drinks']}/{hydration['today']['goal']} | Streak: {hydration['streak']} days")

    if test_mode:
        print("[Test Mode] Configuration loaded successfully. Exiting.")
        return

    try:
        tray_icon = make_tray_icon(config, hydration)
        update_tray_status(tray_icon, hydration)

        scheduler_thread = threading.Thread(target=run_scheduler, args=(config, hydration, tray_icon), daemon=True)
        scheduler_thread.start()

        print("[Tray] Icon launched. Right-click to log drinks or quit.")
        tray_icon.run()
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
