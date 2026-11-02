# Files of the helper (docs/design.md section 2), shipped in the agino-store package. The client's desktop file, icons
# and DConfig metadata are added by the client part.

install(FILES org.aginoos.Store1.conf
        DESTINATION ${CMAKE_INSTALL_DATADIR}/dbus-1/system.d)
install(FILES org.aginoos.Store1.service
        DESTINATION ${CMAKE_INSTALL_DATADIR}/dbus-1/system-services)
install(FILES org.aginoos.Store1.xml
        DESTINATION ${CMAKE_INSTALL_DATADIR}/dbus-1/interfaces)
install(FILES agino-store-helper.service
        DESTINATION ${CMAKE_INSTALL_PREFIX}/lib/systemd/system)
install(FILES org.aginoos.store.policy
        DESTINATION ${CMAKE_INSTALL_DATADIR}/polkit-1/actions)
install(FILES 50-agino-store.rules
        DESTINATION ${CMAKE_INSTALL_DATADIR}/polkit-1/rules.d)
install(FILES store.conf
        DESTINATION /etc/agino-store)

# Trusted catalog public keys (*.pub) are provided by the infra package; create the empty directory.
install(DIRECTORY DESTINATION ${CMAKE_INSTALL_DATADIR}/agino-store/keys)
