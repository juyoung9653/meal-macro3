"""Release version shared by the desktop runtime and Windows packaging."""

APP_VERSION = "0.1.4"
WINDOWS_VERSION = tuple(int(part) for part in APP_VERSION.split(".")) + (0,)
