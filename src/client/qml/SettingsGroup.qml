// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Titled card of settings rows (children go into the card).
ColumnLayout {
    id: group

    property string title
    default property alias rows: inner.data

    spacing: 6

    // Explicit assignment keeps the title and card out of the default property alias.
    data: [
        Muted {
            text: group.title
            Layout.leftMargin: 4
        },
        Control {
            id: card
            Layout.fillWidth: true
            padding: 0
            background: Rectangle {
                radius: 12
                color: card.palette.base
                border.width: 1
                border.color: Qt.alpha(card.palette.windowText, 0.1)
            }
            contentItem: ColumnLayout {
                id: inner
                spacing: 0
            }
        }
    ]
}
