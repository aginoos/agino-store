// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import org.deepin.dtk 1.0 as D

// Rounded category button.
Control {
    id: pill

    property string text
    property bool checked: false
    signal clicked()

    leftPadding: 14
    rightPadding: 14
    topPadding: 5
    bottomPadding: 5

    contentItem: D.Label {
        text: pill.text
        textFormat: Text.PlainText
        font.pixelSize: 12
        color: pill.checked ? pill.palette.highlight : pill.palette.windowText
    }
    background: Rectangle {
        radius: height / 2
        color: pill.checked ? Qt.alpha(pill.palette.highlight, 0.14) : Surface.card(pill.palette)
        border.width: 1
        border.color: pill.checked ? pill.palette.highlight : Qt.alpha(pill.palette.windowText, 0.14)
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: pill.clicked()
        }
    }
}
