// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// One setting: label on the left, children (switch, button, value) on the right.
Control {
    id: item

    property string text
    // Marks a setting that is not available yet, e.g. qsTr("Phase 2").
    property string badge
    default property alias trailing: right.data

    Layout.fillWidth: true
    implicitHeight: 46
    leftPadding: 14
    rightPadding: 14

    background: Rectangle {
        visible: item.y > 0.5
        height: 1
        width: parent.width
        color: Surface.line(item.palette)
    }

    contentItem: RowLayout {
        spacing: 12
        opacity: item.badge !== "" ? 0.6 : 1
        D.Label {
            text: item.text
            elide: Text.ElideRight
            Layout.maximumWidth: item.width * 0.6
        }
        Chip {
            visible: item.badge !== ""
            text: item.badge
        }
        Item { Layout.fillWidth: true }
        RowLayout {
            id: right
            spacing: 12
        }
    }
}
