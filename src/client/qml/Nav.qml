// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

// Left rail: home, categories, updates and downloads; catalog status at the bottom.
Control {
    id: nav

    implicitWidth: 200
    padding: 8

    readonly property bool inCategory: Router.query === "" && Router.page === "category"

    background: Rectangle {
        color: Qt.darker(nav.palette.window, 1.04)
        Rectangle {
            anchors.right: parent.right
            width: 1
            height: parent.height
            color: Qt.alpha(nav.palette.windowText, 0.1)
        }
    }

    component GroupLabel: Muted {
        Layout.fillWidth: true
        Layout.topMargin: 10
        Layout.leftMargin: 10
        font: D.DTK.fontManager.t10
    }

    contentItem: ColumnLayout {
        spacing: 0

        Flickable {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentHeight: rail.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            ColumnLayout {
                id: rail
                width: parent.width
                spacing: 2

                NavButton {
                    Layout.fillWidth: true
                    label: qsTr("Home")
                    iconKey: "home"
                    current: Router.query === "" && Router.page === "home"
                    onClicked: Router.go("home")
                }

                GroupLabel { text: qsTr("Categories") }
                Repeater {
                    model: Store.categories
                    NavButton {
                        required property var modelData
                        Layout.fillWidth: true
                        label: modelData.name
                        iconKey: modelData.id
                        current: nav.inCategory && Router.category === modelData.id
                        onClicked: Router.openCategory(modelData.id)
                    }
                }

                GroupLabel { text: qsTr("Mine") }
                NavButton {
                    Layout.fillWidth: true
                    label: qsTr("Updates")
                    iconKey: "updates"
                    badge: Store.updatesCount
                    current: Router.query === "" && Router.page === "installed"
                    onClicked: Router.go("installed")
                }
                NavButton {
                    Layout.fillWidth: true
                    label: qsTr("Downloads")
                    iconKey: "tasks"
                    badge: Store.activeTasks
                    current: Router.query === "" && Router.page === "tasks"
                    onClicked: Router.go("tasks")
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 10
            Layout.topMargin: 8
            spacing: 2

            Muted {
                Layout.fillWidth: true
                text: Store.catalogState === "unavailable" ? qsTr("Catalog unavailable")
                      : qsTr("Catalog %1").arg(Store.revision)
                font.family: "monospace"
            }
            Muted {
                Layout.fillWidth: true
                visible: Store.catalogState !== "unavailable" && Store.catalogState !== "loading"
                text: (Store.catalogState === "expired" ? "● " + qsTr("signature verified, catalog expired")
                                                        : "● " + qsTr("signature verified"))
                color: Store.catalogState === "expired" ? Qt.alpha(nav.palette.windowText, 0.62)
                                                        : nav.palette.highlight
            }
            Muted {
                Layout.fillWidth: true
                visible: text !== ""
                text: Store.syncText
            }
        }
    }
}
