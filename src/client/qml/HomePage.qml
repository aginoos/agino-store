// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Banner, essentials and one panel per category.
PageBase {
    id: page

    readonly property var banners: Store.featured.filter(function(f) { return f.banner })
    readonly property var essentials: {
        const plain = Store.featured.filter(function(f) { return !f.banner })
        return plain.length > 0 ? plain[0] : null
    }

    AppListModel {
        id: essentialsModel
        appIds: page.essentials ? page.essentials.apps.slice(0, 6) : []
    }

    Banner {
        Layout.fillWidth: true
        visible: page.banners.length > 0
        items: page.banners
    }

    ColumnLayout {
        Layout.fillWidth: true
        visible: page.essentials !== null && essentialsModel.count > 0
        spacing: 12

        RowLayout {
            Layout.fillWidth: true
            Heading {
                Layout.fillWidth: true
                text: page.essentials ? page.essentials.title : ""
            }
            D.ToolButton {
                text: qsTr("See all")
                onClicked: {
                    const apps = page.essentials ? page.essentials.apps : []
                    const cats = apps.length > 0 ? Store.app(apps[0]).categories : []
                    if (cats && cats.length > 0)
                        Router.openCategory(cats[0])
                }
            }
        }
        AppGrid {
            Layout.fillWidth: true
            listModel: essentialsModel
        }
    }

    GridLayout {
        Layout.fillWidth: true
        columns: width > 720 ? 2 : 1
        uniformCellWidths: true
        columnSpacing: 16
        rowSpacing: 16

        Repeater {
            model: Store.categories
            CategoryPanel {
                required property var modelData
                categoryId: modelData.id
                title: modelData.name
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
            }
        }
    }
}
