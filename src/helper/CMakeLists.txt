# agino-store-helper: root D-Bus service (docs/design.md section 5).

# PackageKit-Qt6 (Debian: libpackagekitqt6-dev). Its installed config exports PK::packagekitqt6.
find_package(packagekitqt6 REQUIRED)
if(TARGET PK::packagekitqt6)
    set(AGINO_STORE_PK_TARGET PK::packagekitqt6)
elseif(TARGET packagekitqt6)
    set(AGINO_STORE_PK_TARGET packagekitqt6)
else()
    message(FATAL_ERROR "PackageKit-Qt6 found but no PK::packagekitqt6 target")
endif()

# Adaptor generated from the interface contract. The slots are implemented by StoreService.
qt_add_dbus_adaptor(AGINO_STORE_ADAPTOR_SRCS
    ${CMAKE_SOURCE_DIR}/data/org.aginoos.Store1.xml
    storeservice.h AginoStore::StoreService
    storeadaptor StoreAdaptor)

add_executable(agino-store-helper
    main.cpp
    config.h config.cpp
    fsutil.h fsutil.cpp
    dpkgutil.h dpkgutil.cpp
    installedstore.h installedstore.cpp
    catalogcache.h catalogcache.cpp
    catalogrefresher.h catalogrefresher.cpp
    polkit.h polkit.cpp
    pkclient.h pkclient.cpp
    jobrunner.h jobrunner.cpp
    simulator.h simulator.cpp
    storeservice.h storeservice.cpp
    ${AGINO_STORE_ADAPTOR_SRCS}
)
target_include_directories(agino-store-helper PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_BINARY_DIR}
    ${CMAKE_SOURCE_DIR}/src/common)
target_link_libraries(agino-store-helper PRIVATE
    agino-store-common
    Qt6::Core Qt6::DBus Qt6::Network
    ${AGINO_STORE_PK_TARGET})

install(TARGETS agino-store-helper RUNTIME DESTINATION ${CMAKE_INSTALL_LIBEXECDIR})
