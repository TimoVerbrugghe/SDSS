"""Qt front end. The only place in SDSS that may import a third-party package.

Everything here renders `app.core` state and forwards button presses; no decision worth
testing lives in this package, because none of it is observable without a display.
"""
