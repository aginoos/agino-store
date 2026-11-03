// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtQuick.Layouts
import org.deepin.dtk 1.0 as D

PageBase {
    id: page

    property real cacheBytes: 0

    Component.onCompleted: cacheBytes = Store.assetCacheBytes()

    Connections {
        target: Store
        function onAssetTickChanged() { page.cacheBytes = Store.assetCacheBytes() }
    }

    Heading {
        text: qsTr("Settings")
    }

    ColumnLayout {
        Layout.fillWidth: true
        Layout.maximumWidth: 720
        spacing: 20

        SettingsGroup {
            Layout.fillWidth: true
            title: qsTr("Updates")
            SettingsRow {
                text: qsTr("Check for updates automatically")
                D.Switch {
                    checked: Settings.autoCheckUpdates
                    onToggled: Settings.autoCheckUpdates = checked
                }
            }
            SettingsRow {
                text: qsTr("Don’t download on metered networks")
                D.Switch {
                    checked: Settings.noDownloadOnMetered
                    onToggled: Settings.noDownloadOnMetered = checked
                }
            }
            SettingsRow {
                text: qsTr("Notify me about updates")
                D.Switch {
                    checked: Settings.notifyUpdates
                    onToggled: Settings.notifyUpdates = checked
                }
            }
            // Not wired up before phase 2 of the store, so shown disabled.
            SettingsRow {
                text: qsTr("Install store app updates automatically")
                badge: qsTr("Phase 2")
                D.Switch {
                    enabled: false
                    checked: false
                }
            }
        }

        SettingsGroup {
            Layout.fillWidth: true
            title: qsTr("Downloads and installs")
            SettingsRow {
                text: qsTr("Create a desktop icon after installing")
                D.Switch {
                    checked: Settings.createDesktopIcon
                    onToggled: Settings.createDesktopIcon = checked
                }
            }
            SettingsRow {
                text: qsTr("Delete packages after installing")
                D.Switch {
                    checked: Settings.deletePackagesAfterInstall
                    onToggled: Settings.deletePackagesAfterInstall = checked
                }
            }
        }

        SettingsGroup {
            Layout.fillWidth: true
            title: qsTr("Show")
            SettingsRow {
                text: qsTr("Show community-maintained apps")
                D.Switch {
                    checked: Settings.showPersonal
                    onToggled: Settings.showPersonal = checked
                }
            }
            SettingsRow {
                text: qsTr("Show proprietary apps")
                D.Switch {
                    checked: Settings.showProprietary
                    onToggled: Settings.showProprietary = checked
                }
            }
        }

        SettingsGroup {
            Layout.fillWidth: true
            title: qsTr("Storage")
            SettingsRow {
                text: qsTr("Icon and screenshot cache")
                Muted { text: Store.formatBytes(page.cacheBytes) }
                D.Button {
                    text: qsTr("Clear")
                    onClicked: {
                        Store.clearAssetCache()
                        page.cacheBytes = Store.assetCacheBytes()
                    }
                }
            }
            SettingsRow {
                text: qsTr("Software catalog")
                Muted {
                    text: Store.catalogState === "unavailable" ? qsTr("Catalog unavailable")
                          : Store.catalogState === "expired" ? qsTr("Catalog expired")
                          : Store.syncText
                }
                D.Button {
                    text: qsTr("Refresh now")
                    enabled: !Store.refreshing
                    onClicked: Store.refresh()
                }
            }
        }

        SettingsGroup {
            Layout.fillWidth: true
            title: qsTr("About")
            SettingsRow {
                text: qsTr("About")
                Muted { text: Qt.application.version }
            }
        }
    }
}
