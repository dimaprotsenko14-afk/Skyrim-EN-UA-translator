"""
UI Module Entry Point (ui/app_gui.py).
Delegates to the standalone zero-dependency dark-themed Tkinter GUI (skyrim_translator_gui.py).
"""

from skyrim_translator_gui import SkyrimTranslatorApp, launch_gui

__all__ = ["SkyrimTranslatorApp", "launch_gui"]

if __name__ == "__main__":
    launch_gui()
