// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls

// Horizontal strip of screenshots; the language is chosen by the backend (design 4.1).
ListView {
    id: strip

    orientation: ListView.Horizontal
    spacing: 12
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    delegate: Control {
        id: shot

        required property var modelData
        readonly property real ratio: modelData.width > 0 && modelData.height > 0
                                      ? modelData.width / modelData.height : 1.6
        readonly property string path: modelData.thumb ? modelData.thumb : modelData.image
        readonly property string url: {
            // Reading the counter inside the expression keeps the dependency (a bare
            // statement can be dropped by the QML compiler).
            return Store.assetTick >= 0 ? Store.assetUrl(path) : ""
        }

        height: strip.height
        width: Math.round(height * ratio)
        padding: 1

        background: Rectangle {
            radius: 10
            color: shot.palette.base
            border.width: 1
            border.color: Qt.alpha(shot.palette.windowText, 0.1)
        }
        contentItem: Image {
            source: shot.url
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            sourceSize: Qt.size(shot.width * 2, shot.height * 2)
        }
    }
}
