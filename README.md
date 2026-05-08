# KidLauncher ⚡
A high-performance Minecraft launcher built for zero stutters and a clean experience. No bloat, just speed.
## Why use this?
Most launchers are heavy or have bad memory management. KidLauncher uses a custom engine that forces Java to be efficient so you get the best FPS possible.
### 🚀 Key Features
*   **Insane Optimization**: Uses aggressive ZGC/G1GC flags to keep your game from freezing. It hits <10ms pause times.
*   **Auto Discord RPC**: Shows you're playing KidLauncher on Discord with the classic block logo automatically. No setup needed.
*   **Modern UI**: Clean glassmorphism design that actually looks good and runs fast.
*   **Modrinth Support**: Browse and install mods without leaving the launcher.
*   **Microsoft Auth**: Secure login via official OAuth2.
### ⚙️ Optimization Details
I've tuned the JVM args to include:
- `AlwaysPreTouch` (prevents page faults mid-game)
- `ParallelRefProcEnabled`
- `MaxGCPauseMillis=10` (smooth gameplay)
- Hardware-accelerated OpenGL for the UI
---
## Getting Started
1.  Grab the latest `.exe` from releases.
2.  Log in and create a new instance.
3.  Set your RAM (4-6GB is the sweet spot).
4.  Launch and play.
## Building from source
If you want to build it yourself, you'll need Python 3.10+ and Node.js.
```bash
pip install flask webview pypresence minecraft-launcher-lib requests
python launcher.py
Credits
Made by poppedbykid
Built using minecraft-launcher-lib
