// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import org.deepin.dtk 1.0 as D

// Small outlined tag (source, proprietary).
Control {
    id: chip

    property string text
    property bool accent: false

    leftPadding: 6
    rightPadding: 6
    topPadding: 1
    bottomPadding: 1

    contentItem: D.Label {
        text: chip.text
        font: D.DTK.fontManager.t10
        color: chip.accent ? chip.palette.highlight : Qt.alpha(chip.palette.windowText, 0.66)
    }
    background: Rectangle {
        radius: 5
        color: "transparent"
        border.width: 1
        border.color: chip.accent ? Qt.alpha(chip.palette.highlight, 0.45) : Qt.alpha(chip.palette.windowText, 0.2)
    }
}
