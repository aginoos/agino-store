// SPDX-License-Identifier: GPL-3.0-or-later
pragma Singleton
import QtQuick

// Shared fills and status colours (mock --card, --line, --ok, --warn, --closed).
QtObject {
    function dark(p) { return p.window.hslLightness < 0.5 }

    // Card fill: palette.base is almost the window colour in dark mode, so lift it there.
    function card(p) { return dark(p) ? Qt.tint(p.window, Qt.alpha(p.windowText, 0.05)) : p.base }
    function line(p) { return Qt.alpha(p.windowText, 0.09) }

    function ok(p) { return dark(p) ? "#47c27a" : "#1d8f4e" }
    function warn(p) { return dark(p) ? "#e3a13a" : "#b86e00" }
    function closed(p) { return dark(p) ? "#a58cf0" : "#7a5bd0" }
}
