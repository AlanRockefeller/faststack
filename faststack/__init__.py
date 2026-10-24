"""FastStack package initialization."""

import os

# Qt 6.11 can still discover the host GTK platform theme while using the
# offscreen QPA backend, which makes headless startup require a display.
if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
    os.environ["QT_QPA_PLATFORMTHEME"] = "none"
