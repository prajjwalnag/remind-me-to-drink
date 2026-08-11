# Remind me to Drink 💧

A lightweight desktop reminder app that nudges you to stay hydrated throughout the day. Features smart skipping during focus time, escalating reminders, and streak tracking.

## Features

- **Smart Reminders**: Get notified every 90 minutes (configurable) to drink water
- **Quiet Hours**: Disable reminders during sleep (default: 10 PM – 7 AM)
- **Focus Mode**: Automatically skip reminders during meetings or focused work sessions
- **Escalation**: Reminders get more urgent (gentle → normal → urgent) if you keep ignoring them
- **Streak Tracking**: Track how many consecutive days you've met your daily goal
- **System Tray**: Runs quietly in the background with a tray icon for quick logging
- **Daily Goal**: Set a target number of drinks per day and track progress

## Setup

### Requirements
- Python 3.7+
- Windows (currently Windows-only due to tray icon and window detection requirements)

### Installation

1. Clone or download this project
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the app:
   ```bash
   python reminder.py
   ```

The app will create `config.json` and `hydration.db` in the same directory if they don't exist.

### Standalone .exe (no Python required)

If you just want to run the app without installing Python or any dependencies, use the prebuilt standalone executable instead: copy `dist/RemindMeToDrink.exe` anywhere and double-click it. It carries the icon and all dependencies bundled inside, and creates `config.json`/`hydration.db` next to itself on first run — same as the script version.

#### Building it yourself

```bash
pip install -r requirements-build.txt
pyinstaller RemindMeToDrink.spec
```

This produces `dist/RemindMeToDrink.exe` (~14 MB, single file, no console window). The build config lives in `RemindMeToDrink.spec` — re-run the same command after any code change to rebuild.

## Usage

### Starting the App
```bash
python reminder.py
```

A water droplet icon will appear in your system tray.

### Logging a Drink
Right-click the tray icon and select **"I drank water 💧"** to log a drink. The app will:
- Increment your drink counter
- Update your streak if you meet the daily goal
- Reset the escalation level back to gentle

### Checking Status
Right-click the tray icon and select **"Show status"** to open a status window showing:
- Today's progress with a visual progress bar (X/Y drinks)
- Your current streak (consecutive days meeting your goal)
- Time of your last drink
- A quick "I drank water" button right in the window

### Configuration
Edit `config.json` to customize:

- **interval_minutes**: Minutes between reminders (default: 90)
- **daily_goal**: Target number of drinks per day (default: 8)
- **quiet_hours**: Start/end times to skip reminders (default: 22:00–07:00)
- **focus_keywords**: Window titles that trigger focus mode (default: "meeting", "zoom", "vs code", "terminal")
- **escalation_enabled**: Enable escalating urgency for missed reminders (default: true)

Example:
```json
{
  "interval_minutes": 60,
  "daily_goal": 10,
  "quiet_hours": {
    "start": "23:00",
    "end": "08:00"
  },
  "focus_keywords": ["meeting", "zoom", "presentation"],
  "escalation_enabled": true
}
```

Right-click the tray icon and select **"Settings"** to change these values from a settings window (sliders, fields, and a save/cancel flow) instead of editing the JSON file directly.

## Data Storage

All hydration data is stored **locally** — there is no network access, cloud sync, or telemetry anywhere in this app.

- **config.json**: Your personalized settings (interval, goal, quiet hours, focus keywords)
- **hydration.db**: A local SQLite database holding today's progress, streak, and full drink history

`hydration.db` has two tables: `state` (today's counters, streak, last drink time) and `drink_log` (one row per past day, archived automatically at midnight). If you're upgrading from an older version of this app that used `hydration.json`, the app migrates that file into `hydration.db` automatically the first time it runs — the old `hydration.json` is left in place afterward (not deleted) in case you want to double-check the data before removing it yourself.

## Icon Attribution

The water droplet icon is from **Flaticon** and is used under the free license.

**Please attribute:** 
[Icon author/icon name](https://www.flaticon.com/) — [View the original icon page and add the specific author/link from your Flaticon download]

Per Flaticon's free-tier license, please include a link to the specific icon and its author. You can find this information on the Flaticon download page where you got the icon.

## Notifications

The app sends Windows toast notifications with level-appropriate messages:

- **Gentle** (1st missed reminder): "💧 Sip of water? (1st reminder)"
- **Normal** (2nd missed reminder): "💧 Time to hydrate! (3/8 today)"
- **Urgent** (3rd+ missed reminder): "🚨 You've ignored 2 reminders—drink now!"

## Troubleshooting

- **Icon not showing**: Ensure `drop.png` is in the same directory as `reminder.py`
- **No notifications**: Check that notification permissions are enabled in Windows Settings (Settings → System → Notifications)
- **Window detection not working**: The app uses Windows' native window APIs; make sure your active window title is visible and includes the focus keywords you configured
- **Stuck in tray**: Right-click the icon and select "Quit" to exit cleanly

## License

This project is open source. See the icon attribution section for third-party credits.
