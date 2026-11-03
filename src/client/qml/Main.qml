// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

D.ApplicationWindow {
    id: window

    width: 1040
    height: 700
    minimumWidth: 760
    minimumHeight: 520
    visible: true
    title: qsTr("App Store")
    // DTK draws the title bar (D.TitleBar below) instead of the window manager. D.WindowButtonGroup
    // shows each window button only when the window carries the matching hint, and a QML Window
    // starts with plain Qt.Window, so the hints are listed here.
    D.DWindow.enabled: true
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowMinimizeButtonHint
           | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint

    readonly property Component pageComponent: {
        if (Store.catalogState === "unavailable" && Router.page !== "settings")
            return unavailablePage
        if (Router.query !== "")
            return searchPage
        switch (Router.page) {
        case "category": return categoryPage
        case "detail": return detailPage
        case "installed": return installedPage
        case "tasks": return tasksPage
        case "settings": return settingsPage
        default: return homePage
        }
    }

    // Title bar after the deepin store: the left part is the rail's width (app icon and name), a
    // separator continues the rail's edge, then back button, search and the downloads button.
    header: D.TitleBar {
        id: titleBar

        leftContent: RowLayout {
            spacing: 0
            Rectangle {
                Layout.preferredWidth: 200 - 10
                Layout.fillHeight: true
                color: Qt.darker(window.palette.window, 1.04)
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 10
                    spacing: 8
                    D.DciIcon {
                        name: "agino-store"
                        sourceSize: Qt.size(24, 24)
                        Layout.preferredWidth: 24
                        Layout.preferredHeight: 24
                    }
                    D.Label {
                        text: window.title
                        font: D.DTK.fontManager.t6
                    }
                }
            }
            Rectangle {
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                color: Qt.alpha(window.palette.windowText, 0.1)
            }
            D.IconButton {
                Layout.leftMargin: 14
                icon.name: "arrow_ordinary_left"
                enabled: Router.history.length > 0
                onClicked: Router.back()
            }
        }

        content: Item {
            RowLayout {
                anchors.left: parent.left
                anchors.leftMargin: 4
                anchors.verticalCenter: parent.verticalCenter
                spacing: 14
                D.SearchEdit {
                    id: search
                    Layout.preferredWidth: Math.max(160, Math.min(340, titleBar.width - 560))
                    placeholder: qsTr("Search apps")
                    onTextChanged: debounce.restart()
                }
                // Downloads: accent circle with a badge while tasks are running.
                Item {
                    Layout.preferredWidth: 32
                    Layout.preferredHeight: 32
                    Rectangle {
                        anchors.fill: parent
                        radius: width / 2
                        color: window.palette.highlight
                        NavIcon {
                            anchors.centerIn: parent
                            key: "tasks"
                            color: window.palette.highlightedText
                        }
                    }
                    Rectangle {
                        visible: Store.activeTasks > 0
                        x: parent.width - 10
                        y: -4
                        width: Math.max(16, badgeText.implicitWidth + 8)
                        height: 16
                        radius: 8
                        color: "#e5484d"
                        Text {
                            id: badgeText
                            anchors.centerIn: parent
                            text: Store.activeTasks
                            color: "#ffffff"
                            font.pixelSize: 11
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: Router.go("tasks")
                    }
                }
            }
            Timer {
                id: debounce
                interval: 220
                onTriggered: Router.query = search.text.trim()
            }
            Connections {
                target: Router
                function onQueryChanged() {
                    if (Router.query === "" && search.text !== "")
                        search.text = ""
                }
            }
        }

        menu: D.Menu {
            D.Action {
                text: qsTr("Settings")
                onTriggered: Router.go("settings")
            }
            D.ThemeMenu {}
            D.MenuSeparator {}
            D.AboutAction {
                // TODO(verify): AboutDialog property names (productName, productIcon, version, websiteName, websiteLink, license).
                aboutDialog: D.AboutDialog {
                    productName: qsTr("App Store")
                    productIcon: "agino-store"
                    version: Qt.application.version
                    websiteName: "aginoos.com"
                    websiteLink: "https://aginoos.com"
                    license: "GPL-3.0-or-later"
                }
            }
            D.QuitAction {}
        }
    }

    Component { id: homePage; HomePage {} }
    Component { id: categoryPage; CategoryPage {} }
    Component { id: searchPage; SearchPage {} }
    Component { id: detailPage; DetailPage {} }
    Component { id: installedPage; InstalledPage {} }
    Component { id: tasksPage; TasksPage {} }
    Component { id: settingsPage; SettingsPage {} }
    Component { id: unavailablePage; UnavailablePage {} }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Nav {
            Layout.fillHeight: true
            Layout.preferredWidth: 200
        }
        Loader {
            Layout.fillWidth: true
            Layout.fillHeight: true
            sourceComponent: window.pageComponent
        }
    }

    ConfirmDialog {
        id: confirmDialog
    }

    Toast {
        id: toast
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 18
    }

    Connections {
        target: Store
        function onConfirmRequested(request) { confirmDialog.show(request) }
        function onToast(text) { toast.show(text) }
    }
}
