// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Scrolling page with padding. Children go into the column (default property).
Item {
    id: root

    default property alias pageContent: column.data
    property int pad: 26
    readonly property real contentWidth: scroll.availableWidth - 2 * pad

    // Explicit assignment keeps the scroll view out of the default property alias.
    data: [
        D.ScrollView {
            id: scroll
            anchors.fill: parent
            clip: true
            contentWidth: availableWidth
            contentHeight: column.implicitHeight + 2 * root.pad

            ColumnLayout {
                id: column
                x: root.pad
                y: root.pad
                width: root.contentWidth
                spacing: 26
            }
        }
    ]
}
