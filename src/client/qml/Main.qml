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
    // DTK draws the title bar (D.TitleBar below) instead of the window manager.
    D.DWindow.enabled: true

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

    header: D.TitleBar {
        icon.name: "agino-store"

        // TODO(verify): DTK icon name of the "back" arrow.
        leftContent: D.IconButton {
            icon.name: "arrow_ordinary_left"
            enabled: Router.history.length > 0
            onClicked: Router.back()
        }

        content: Item {
            D.SearchEdit {
                id: search
                anchors.centerIn: parent
                width: Math.min(360, parent.width - 24)
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
