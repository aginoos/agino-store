// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Home page panel of one category: five apps, popular or new.
Control {
    id: panel

    property string categoryId
    property string title
    property string mode: "hot"

    visible: list.count > 0
    leftPadding: 14
    rightPadding: 14
    topPadding: 12
    bottomPadding: 4

    background: Rectangle {
        radius: 12
        color: panel.palette.base
        border.width: 1
        border.color: Qt.alpha(panel.palette.windowText, 0.1)
    }

    AppListModel {
        id: list
        category: panel.categoryId
        sort: panel.mode
        limit: 5
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 8
            spacing: 10
            Heading {
                Layout.fillWidth: true
                text: panel.title
                font: D.DTK.fontManager.t6
                font.weight: Font.DemiBold
            }
            SegmentedSwitch {
                mode: panel.mode
                onPicked: function(m) { panel.mode = m }
            }
            D.ToolButton {
                text: qsTr("See all")
                onClicked: Router.openCategory(panel.categoryId)
            }
        }

        Repeater {
            model: list
            AppRow {
                appId: model.appId
                Layout.fillWidth: true
            }
        }
    }
}
