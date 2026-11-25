cmake_minimum_required(VERSION 3.20)
project(zlibdemo_cmake C)

# Download and build a specific zlib version as part of this project.
include(FetchContent)

set(ZLIB_VERSION 1.3.1)
set(ZLIB_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)

FetchContent_Declare(
    zlib
    URL https://github.com/madler/zlib/releases/download/v${ZLIB_VERSION}/zlib-${ZLIB_VERSION}.tar.gz
    URL_HASH SHA256=9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23
)
FetchContent_MakeAvailable(zlib)

add_executable(zlibdemo src/main.c)

# Release: -DNDEBUG, assertions disabled. Debug: -DDEBUG, assertions active.
if(MSVC)
    set(DEBUG_FLAGS /Od /Zi)
    set(RELEASE_FLAGS /O2)
else()
    set(DEBUG_FLAGS -g -O0)
    set(RELEASE_FLAGS -O2)
endif()

target_compile_definitions(zlibdemo PRIVATE
    $<$<CONFIG:Debug>:DEBUG>
    $<$<CONFIG:Release>:NDEBUG>
)
target_compile_options(zlibdemo PRIVATE
    $<$<CONFIG:Debug>:${DEBUG_FLAGS}>
    $<$<CONFIG:Release>:${RELEASE_FLAGS}>
)

# zlib 1.3.1 exports plain targets; newer versions also provide ZLIB::ZLIB.
if(TARGET ZLIB::ZLIB)
    target_link_libraries(zlibdemo PRIVATE ZLIB::ZLIB)
elseif(TARGET zlibstatic)
    target_link_libraries(zlibdemo PRIVATE zlibstatic)
    target_include_directories(zlibdemo PRIVATE ${zlib_SOURCE_DIR} ${zlib_BINARY_DIR})
else()
    message(FATAL_ERROR "No zlib target found")
endif()
