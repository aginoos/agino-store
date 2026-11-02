# SPDX-License-Identifier: GPL-3.0-or-later
set(TEST_DATA_DIR "${CMAKE_CURRENT_SOURCE_DIR}/data")

foreach(name test_catalog test_manifest)
    qt_add_executable(${name} ${name}.cpp)
    target_link_libraries(${name} PRIVATE agino-store-common Qt6::Test)
    target_compile_definitions(${name} PRIVATE TEST_DATA_DIR="${TEST_DATA_DIR}")
    add_test(NAME ${name} COMMAND ${name})
endforeach()
