// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Label / value list of the detail page: [{label, value, mono}].
Control {
    id: panel

    property var rows: []

    leftPadding: 14
    rightPadding: 14
    topPadding: 4
    bottomPadding: 4

    background: Rectangle {
        radius: 12
        color: panel.palette.base
        border.width: 1
        border.color: Qt.alpha(panel.palette.windowText, 0.1)
    }

    contentItem: ColumnLayout {
        spacing: 0
        Repeater {
            model: panel.rows
            ColumnLayout {
                id: entry
                required property int index
                required property var modelData
                Layout.fillWidth: true
                spacing: 0

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    visible: entry.index > 0
                    color: Qt.alpha(panel.palette.windowText, 0.1)
                }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 7
                    Layout.bottomMargin: 7
                    spacing: 12
                    Muted {
                        text: entry.modelData.label
                        elide: Text.ElideNone
                    }
                    D.Label {
                        Layout.fillWidth: true
                        horizontalAlignment: Text.AlignRight
                        wrapMode: Text.WrapAnywhere
                        text: entry.modelData.value
                        font: Qt.font({ family: entry.modelData.mono ? "monospace" : D.DTK.fontManager.t8.family, pixelSize: D.DTK.fontManager.t8.pixelSize, weight: D.DTK.fontManager.t8.weight })
                    }
                }
            }
        }
    }
}
