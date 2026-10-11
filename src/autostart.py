from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT_DIR

STARTUP_NAME = "PC Admin Bot.lnk"
LEGACY_NAMES = (
    "PC Admin Bot.vbs",
    "PC Admin Bot.bat",
)


def startup_dir() -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def _create_shortcut(lnk_path: Path, target: str, arguments: str, work_dir: str) -> None:
    """Create a normal Windows .lnk in Startup (not a dropped .vbs/.bat)."""
    icon = str(ROOT_DIR / "assets" / "icon.ico")
    env = os.environ.copy()
    env["PAB_LNK"] = str(lnk_path)
    env["PAB_TARGET"] = target
    env["PAB_ARGS"] = arguments
    env["PAB_WORKDIR"] = work_dir
    env["PAB_ICON"] = icon

    # Paths via env vars — no string concatenation / encoded commands.
    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        "$l = $s.CreateShortcut($env:PAB_LNK); "
        "$l.TargetPath = $env:PAB_TARGET; "
        "$l.Arguments = $env:PAB_ARGS; "
        "$l.WorkingDirectory = $env:PAB_WORKDIR; "
        "$l.Description = 'PC Admin Bot'; "
        "$l.IconLocation = $env:PAB_ICON; "
        "$l.Save()"
    )
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps,
        ],
        check=True,
        env=env,
        creationflags=flags,
    )


def install() -> Path:
    run_vbs = (ROOT_DIR / "scripts" / "run.vbs").resolve()
    if not run_vbs.is_file():
        raise FileNotFoundError(f"Missing launcher: {run_vbs}")

    pythonw = ROOT_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        raise FileNotFoundError(
            f"Missing {pythonw}. Create .venv and pip install -r requirements.txt"
        )

    startup = startup_dir()
    startup.mkdir(parents=True, exist_ok=True)

    for name in LEGACY_NAMES:
        legacy = startup / name
        if legacy.exists():
            legacy.unlink()

    lnk_path = startup / STARTUP_NAME
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    wscript = system_root / "System32" / "wscript.exe"
    _create_shortcut(
        lnk_path,
        target=str(wscript),
        arguments=f'//nologo "{run_vbs}"',
        work_dir=str(ROOT_DIR),
    )
    if not lnk_path.is_file():
        raise RuntimeError(f"Shortcut was not created: {lnk_path}")
    return lnk_path


def uninstall() -> list[Path]:
    startup = startup_dir()
    removed: list[Path] = []
    for name in (STARTUP_NAME, *LEGACY_NAMES):
        path = startup / name
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PC Admin Bot Windows autostart")
    parser.add_argument("action", choices=("install", "uninstall", "path", "manual"))
    args = parser.parse_args(argv)

    if args.action == "path":
        print(startup_dir())
        return 0

    if args.action == "manual":
        folder = startup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        print("Открываю папку автозагрузки Windows.")
        print("Создай ярлык на файл:")
        print(f"  {(ROOT_DIR / 'scripts' / 'run.vbs').resolve()}")
        print(f"и положи его сюда:\n  {folder}")
        os.startfile(folder)  # noqa: S606
        return 0

    if args.action == "install":
        path = install()
        print(f"Autostart shortcut installed:\n  {path}")
        return 0

    removed = uninstall()
    if removed:
        for path in removed:
            print(f"Removed: {path}")
    else:
        print("Autostart entry not found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
