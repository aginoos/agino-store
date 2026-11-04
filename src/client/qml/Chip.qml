// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import org.deepin.dtk 1.0 as D

// Small outlined tag (source, proprietary).
Control {
    id: chip

    property string text
    // "", "accent", "ok", "warn" or "closed"; empty is the neutral grey.
    property string tone: ""
    readonly property color toneColor: tone === "ok" ? Surface.ok(palette)
                                       : tone === "warn" ? Surface.warn(palette)
                                       : tone === "closed" ? Surface.closed(palette)
                                       : tone === "accent" ? palette.highlight
                                       : Qt.alpha(palette.windowText, 0.62)

    leftPadding: 6
    rightPadding: 6
    topPadding: 1
    bottomPadding: 1

    contentItem: D.Label {
        text: chip.text
        textFormat: Text.PlainText
        font.pixelSize: 11
        color: chip.toneColor
    }
    background: Rectangle {
        radius: 5
        color: "transparent"
        border.width: 1
        border.color: chip.tone !== "" ? Qt.alpha(chip.toneColor, 0.4) : Qt.alpha(chip.palette.windowText, 0.2)
    }
}
