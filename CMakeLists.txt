cmake_minimum_required(VERSION 3.25)
project(agino-store VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

include(GNUInstallDirs)

find_package(Qt6 6.8 REQUIRED COMPONENTS Core DBus Network Gui Qml Quick LinguistTools)
find_package(OpenSSL 3 REQUIRED)

option(BUILD_TESTING "Build the unit tests" ON)

add_subdirectory(src/common)
add_subdirectory(src/helper)
add_subdirectory(src/client)
add_subdirectory(data)

if(BUILD_TESTING)
    enable_testing()
    find_package(Qt6 REQUIRED COMPONENTS Test)
    add_subdirectory(tests)
endif()
