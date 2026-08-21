"""Entry point for the packaged executable: straight into the GUI."""

from notion2mnemo.gui.app import run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui())
