# SPDX-License-Identifier: GPL-3.0-or-later
add_library(agino-store-common STATIC
    catalog.cpp
    catalog.h
    manifest.cpp
    manifest.h
)
set_target_properties(agino-store-common PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(agino-store-common PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(agino-store-common PUBLIC Qt6::Core OpenSSL::Crypto)
