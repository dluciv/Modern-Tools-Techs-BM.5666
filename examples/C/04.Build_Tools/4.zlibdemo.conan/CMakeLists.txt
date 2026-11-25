cmake_minimum_required(VERSION 3.20)
project(zlibdemo_conan C)

# Conan generates ZLIBConfig.cmake via CMakeDeps.
find_package(ZLIB REQUIRED)

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

target_link_libraries(zlibdemo PRIVATE ZLIB::ZLIB)
