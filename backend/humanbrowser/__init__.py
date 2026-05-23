"""humanbrowser — a human-grade, scriptable & agent-drivable browser.

Public surface:
    from humanbrowser import HumanBrowser, BrowserConfig
"""
from .browser import HumanBrowser, ActionError
from .config import BrowserConfig
from .context import PageContext

__all__ = ["HumanBrowser", "BrowserConfig", "PageContext", "ActionError"]
