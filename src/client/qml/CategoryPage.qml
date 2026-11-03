// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts

// One category: category buttons and the app grid.
PageBase {
    id: page

    readonly property var currentCategory: {
        const found = Store.categories.filter(function(c) { return c.id === Router.category })
        return found.length > 0 ? found[0] : null
    }

    AppListModel {
        id: list
        category: Router.category
        sort: "hot"
    }

    Heading {
        text: page.currentCategory ? page.currentCategory.name : ""
    }

    Flow {
        Layout.fillWidth: true
        spacing: 8
        Repeater {
            model: Store.categories
            Pill {
                required property var modelData
                text: modelData.name
                checked: modelData.id === Router.category
                onClicked: Router.openCategory(modelData.id)
            }
        }
    }

    AppGrid {
        Layout.fillWidth: true
        listModel: list
    }

    Muted {
        Layout.alignment: Qt.AlignHCenter
        Layout.topMargin: 30
        horizontalAlignment: Text.AlignHCenter
        font.pixelSize: 14
        visible: list.count === 0
        text: qsTr("No matching apps")
    }
}
