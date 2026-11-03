// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Card holding a vertical list of rows for a model; the delegate is a ListItem.
Control {
    id: box

    property var listModel
    property Component delegate

    padding: 0

    background: Rectangle {
        radius: 12
        color: Surface.card(box.palette)
        border.width: 1
        border.color: Surface.line(box.palette)
    }

    contentItem: ColumnLayout {
        spacing: 0
        Repeater {
            model: box.listModel
            delegate: box.delegate
        }
    }
}
