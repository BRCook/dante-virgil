"""Paths shared by virgild and virgilctl, kept apart so virgilctl doesn't load the whole daemon."""

from pathlib import Path

# Local control, for virgilctl: only root and dante-virgil can open it.
CONTROL_SOCKET = Path("/run/dante-virgil/control.sock")
LEAD_PATH = "/control/v1/lead"
HANDOVER_PATH = "/group/v1/handover"
