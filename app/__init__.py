"""The SDSS desktop app.

`app.core` is standard-library only and holds every decision the app makes, so it is
unit-testable on a CI runner with no display and no SteamOS. `app.ui` is a thin Qt layer
that renders that state and forwards button presses, and is the only place a third-party
dependency (PySide6, bundled inside the AppImage) may be imported.
"""
