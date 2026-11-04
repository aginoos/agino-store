// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import org.deepin.dtk 1.0 as D

// An app's icon from the catalog; a tinted initial stands in until the download is done.
Control {
    id: root

    property string appId
    property int size: 48

    // Re-evaluated when an asset arrives.
    readonly property string source: {
        // Reading the counter inside the expression keeps the dependency (a bare
        // statement can be dropped by the QML compiler).
        return Store.assetTick >= 0 ? Store.iconUrl(appId) : ""
    }
    readonly property string initial: {
        const name = Store.app(appId).name
        return name ? name.charAt(0).toUpperCase() : "?"
    }

    implicitWidth: size
    implicitHeight: size
    padding: 0

    contentItem: Item {
        Image {
            id: image
            anchors.fill: parent
            source: root.source
            sourceSize: Qt.size(root.size * 2, root.size * 2)
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            visible: status === Image.Ready
        }
        Rectangle {
            anchors.fill: parent
            visible: !image.visible
            radius: root.size * 0.22
            color: Qt.alpha(root.palette.highlight, 0.16)
            D.Label {
                anchors.centerIn: parent
                text: root.initial
                textFormat: Text.PlainText
                color: root.palette.highlight
                font.pixelSize: root.size * 0.46
                font.weight: Font.DemiBold
            }
        }
    }
}
