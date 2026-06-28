"""
Telethon-based Forum Forwarder — importable module.
Wraps the CLI forwarder logic for use inside the Flask web app.
"""
from .runner import ForwarderRunner, get_runner

__all__ = ["ForwarderRunner", "get_runner"]
