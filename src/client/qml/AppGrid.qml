// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts

// Responsive grid of app cards for an AppListModel.
GridLayout {
    id: grid

    property var listModel
    readonly property int count: listModel ? listModel.count : 0

    columns: Math.max(1, Math.floor(width / 260))
    uniformCellWidths: true
    columnSpacing: 12
    rowSpacing: 12

    Repeater {
        model: grid.listModel
        AppCard {
            appId: model.appId
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
        }
    }
}
