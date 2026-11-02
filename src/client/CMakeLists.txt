# SPDX-License-Identifier: GPL-3.0-or-later
# agino-store: the QML/DTK client (docs/design.md section 8).

find_package(Dtk6 REQUIRED COMPONENTS Core)

# Put the module under qrc:/qt/qml so engine.loadFromModule() finds it in the default import path.
qt_policy(SET QTP0001 NEW)

# Singletons must be flagged before the module is created.
set_source_files_properties(qml/Router.qml qml/Icons.qml PROPERTIES QT_QML_SINGLETON_TYPE TRUE)

set(AGINO_STORE_QML_FILES
    qml/Main.qml
    qml/Router.qml
    qml/Icons.qml
    qml/PageBase.qml
    qml/Muted.qml
    qml/Heading.qml
    qml/Chip.qml
    qml/Pill.qml
    qml/SourceChips.qml
    qml/AppIcon.qml
    qml/ActionButtons.qml
    qml/SegmentedSwitch.qml
    qml/AppCard.qml
    qml/AppRow.qml
    qml/AppGrid.qml
    qml/NavIcon.qml
    qml/NavButton.qml
    qml/Nav.qml
    qml/Banner.qml
    qml/CategoryPanel.qml
    qml/HomePage.qml
    qml/CategoryPage.qml
    qml/SearchPage.qml
    qml/Screenshots.qml
    qml/InfoPanel.qml
    qml/DetailPage.qml
    qml/TabHeader.qml
    qml/ListColumn.qml
    qml/ListItem.qml
    qml/InstalledPage.qml
    qml/TaskRow.qml
    qml/TasksPage.qml
    qml/SettingsGroup.qml
    qml/SettingsRow.qml
    qml/SettingsPage.qml
    qml/ConfirmDialog.qml
    qml/Toast.qml
    qml/UnavailablePage.qml
)
# The files live in qml/ but are served from the module root next to its qmldir: the implicit
# directory import of each file then includes the module and its C++ types.
foreach(qml_file IN LISTS AGINO_STORE_QML_FILES)
    get_filename_component(qml_name ${qml_file} NAME)
    set_source_files_properties(${qml_file} PROPERTIES QT_RESOURCE_ALIAS ${qml_name})
endforeach()

qt_add_executable(agino-store main.cpp)

# D-Bus proxy of the helper interface: store_interface.h / .cpp in the build directory.
qt_add_dbus_interface(AGINO_STORE_DBUS_SRCS
    ${PROJECT_SOURCE_DIR}/data/org.aginoos.Store1.xml store_interface)

qt_add_qml_module(agino-store
    URI org.aginoos.store
    VERSION 1.0
    SOURCES
        storeconfig.h storeconfig.cpp
        catalogcache.h catalogcache.cpp
        assetcache.h assetcache.cpp
        helperclient.h helperclient.cpp
        taskmodel.h taskmodel.cpp
        appsettings.h appsettings.cpp
        desktopicons.h desktopicons.cpp
        applistmodel.h applistmodel.cpp
        store.h store.cpp storeactions.cpp
        ${AGINO_STORE_DBUS_SRCS}
    QML_FILES ${AGINO_STORE_QML_FILES}
)

target_include_directories(agino-store PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_BINARY_DIR}
    ${PROJECT_SOURCE_DIR}/src/common)

target_compile_definitions(agino-store PRIVATE AGINO_STORE_VERSION="${PROJECT_VERSION}")

target_link_libraries(agino-store PRIVATE
    agino-store-common
    Qt6::Quick Qt6::Qml Qt6::DBus Qt6::Network Qt6::Gui
    Dtk6::Core)

# Source strings are English; Chinese lives in translations/agino-store_zh_CN.ts and is compiled
# into the executable (resource prefix /i18n, loaded in main.cpp).
qt_add_translations(agino-store
    TS_FILES ${PROJECT_SOURCE_DIR}/translations/agino-store_zh_CN.ts)

install(TARGETS agino-store RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(FILES data/agino-store.desktop DESTINATION ${CMAKE_INSTALL_DATADIR}/applications)
install(FILES data/agino-store.svg
        DESTINATION ${CMAKE_INSTALL_DATADIR}/icons/hicolor/scalable/apps)
install(FILES data/org.aginoos.store.json
        DESTINATION ${CMAKE_INSTALL_DATADIR}/dsg/configs/org.aginoos.store)
