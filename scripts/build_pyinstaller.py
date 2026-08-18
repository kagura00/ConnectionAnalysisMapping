"""Run PyInstaller with a Windows-compatible PE post-processing policy.

Some Windows endpoint environments intermittently deny or remove a freshly
assembled one-file executable while PyInstaller rewrites its PE timestamp and
checksum. Those fields are optional for the installer; the external SHA-256
manifest provides the release integrity check. Keep the workaround isolated to
the build helper so normal PyInstaller behavior is unchanged for users.
"""

from __future__ import annotations

import PyInstaller.building.api as building_api


def main() -> None:
    building_api.winutils.set_exe_build_timestamp = lambda *args: None
    building_api.winutils.update_exe_pe_checksum = lambda *args: None

    from PyInstaller.__main__ import run

    run()


if __name__ == "__main__":
    main()
