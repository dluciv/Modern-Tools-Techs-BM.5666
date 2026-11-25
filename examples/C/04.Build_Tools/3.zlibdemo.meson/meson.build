project('zlibdemo-meson', 'c', version: '1.0')

# Release: -DNDEBUG, assertions disabled. Debug: -DDEBUG, assertions active.
# Optimization and debug-info options are set by meson setup (see __build__.sh).
if get_option('buildtype').startswith('debug')
  add_project_arguments('-DDEBUG', language: 'c')
else
  add_project_arguments('-DNDEBUG', language: 'c')
endif

# Download zlib through Meson's WrapDB as a project subproject.
zlib_dep = dependency(
  'zlib',
  fallback: ['zlib', 'zlib_dep'],
  default_options: ['tests=disabled'],
)

executable('zlibdemo', 'src/main.c', dependencies: zlib_dep)
