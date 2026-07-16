"""PyInstaller entry point for Headroom's private desktop engine."""

from headroom.desktop_bridge import cli_main


if __name__ == "__main__":
    raise SystemExit(cli_main())
