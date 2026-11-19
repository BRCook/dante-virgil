"""Paths shared by virgild and virgilctl, kept apart so virgilctl doesn't load the whole daemon."""

from pathlib import Path

# Local control, for virgilctl: only root and dante-virgil can open it.
CONTROL_SOCKET = Path("/run/dante-virgil/control.sock")
LEAD_PATH = "/control/v1/lead"
REMOVE_PATH = "/control/v1/remove"
LEAVE_PATH = "/control/v1/leave"
HANDOVER_PATH = "/group/v1/handover"
