"""Decky backend for the SDSS second-screen toggles.

Everything routes through the `sdss` CLI so emulator config edits keep going through
the patch journal — the plugin never touches emulator configuration itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import decky


class SdssError(RuntimeError):
    pass


# Every sdss subcommand the plugin issues is a config edit or a status read; none of them
# should take anywhere near this long.
COMMAND_TIMEOUT = 15.0


class Plugin:
    def _sdss(self) -> str:
        found = shutil.which("sdss")
        if found:
            return found
        fallback = Path.home() / ".local/bin/sdss"
        if fallback.is_file():
            return str(fallback)
        raise SdssError("sdss is not installed; run the SDSS installer on this machine")

    async def _run(self, *args: str) -> str:
        env = dict(os.environ)
        env.setdefault("HOME", str(Path.home()))
        # Decky Loader is a PyInstaller-frozen binary; it sets LD_LIBRARY_PATH to its own
        # bundled extraction dir (/tmp/_MEI...) so its embedded Python can find its bundled
        # libs. Inheriting that into `sdss` (a bash shim that execs system bash/python3)
        # makes those binaries link against the wrong libreadline/libc and crash with a
        # symbol lookup error before sdss ever runs. PyInstaller restores the pre-launch
        # value under LD_LIBRARY_PATH_ORIG when one existed; fall back to unset otherwise.
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
        process = await asyncio.create_subprocess_exec(
            self._sdss(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            # Without a timeout a hung `sdss` (a stuck podman pull, an unreachable
            # emulator config on a network mount) blocks the Decky UI forever with no way
            # out but rebooting Steam.
            stdout, stderr = await asyncio.wait_for(process.communicate(), COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            # Reap it so the killed child does not linger as a zombie.
            await process.wait()
            raise SdssError(
                f"sdss {' '.join(args)} timed out after {COMMAND_TIMEOUT:g}s"
            ) from None
        if process.returncode:
            raise SdssError(stderr.decode().strip() or f"sdss {' '.join(args)} failed")
        return stdout.decode()

    async def get_state(self) -> dict:
        try:
            return json.loads(await self._run("status", "--json"))
        except SdssError as error:
            decky.logger.error("sdss status failed: %s", error)
            return {"enabled": False, "profiles": [], "error": str(error)}
        except json.JSONDecodeError:
            decky.logger.error("sdss status returned invalid JSON")
            return {"enabled": False, "profiles": [], "error": "invalid sdss output"}

    async def set_enabled(self, enabled: bool, profile: str | None = None) -> dict:
        args = ["enable" if enabled else "disable"]
        if profile:
            args += ["--profile", profile]
        try:
            await self._run(*args)
        except SdssError as error:
            decky.logger.error("sdss %s failed: %s", args, error)
            state = await self.get_state()
            state["error"] = str(error)
            return state
        return await self.get_state()

    async def restore(self) -> dict:
        """Undo every emulator config edit recorded in the patch journal."""
        try:
            await self._run("restore")
        except SdssError as error:
            state = await self.get_state()
            state["error"] = str(error)
            return state
        return await self.get_state()

    async def _main(self) -> None:
        decky.logger.info("SDSS plugin loaded")

    async def _unload(self) -> None:
        decky.logger.info("SDSS plugin unloaded")
