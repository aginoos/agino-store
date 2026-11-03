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

    // Title bar after the deepin store: over the rail (200 wide) the app icon and name, the rail's
    // separator continues up, and right of it come the back button and the search. Everything sits
    // in the content area, positioned from the title bar's left edge.
    header: D.TitleBar {
        id: titleBar

        content: Item {
            id: bar
            // Offset of the title bar's left edge inside this item (kept in step with layout changes).
            property real originX: 0
            function syncOrigin() { originX = -mapToItem(titleBar, 0, 0).x }
            Component.onCompleted: syncOrigin()
            onXChanged: syncOrigin()
            onWidthChanged: syncOrigin()
            Connections {
                target: titleBar
                function onWidthChanged() { bar.syncOrigin() }
            }

            Rectangle {
                x: bar.originX
                width: 199
                height: parent.height
                color: Qt.darker(window.palette.window, 1.04)
            }
            RowLayout {
                x: bar.originX + 10
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8
                D.DciIcon {
                    name: "agino-store"
                    sourceSize: Qt.size(24, 24)
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: 24
                    Layout.alignment: Qt.AlignVCenter
                }
                D.Label {
                    Layout.alignment: Qt.AlignVCenter
                    text: window.title
                    font: D.DTK.fontManager.t6
                }
            }
            Rectangle {
                x: bar.originX + 199
                z: 10
                width: 1
                anchors.verticalCenter: parent.verticalCenter
                height: parent.height
                color: Qt.alpha(window.palette.windowText, 0.22)
            }
            D.IconButton {
                id: backButton
                x: bar.originX + 200 + 14
                anchors.verticalCenter: parent.verticalCenter
                icon.name: "arrow_ordinary_left"
                enabled: Router.history.length > 0
                onClicked: Router.back()
            }
            D.SearchEdit {
                id: search
                x: backButton.x + backButton.width + 10
                anchors.verticalCenter: parent.verticalCenter
                width: Math.max(160, Math.min(340, parent.width - x - 20))
                placeholder: qsTr("Search apps")
                onTextChanged: debounce.restart()
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
