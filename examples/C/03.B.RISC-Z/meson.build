project('RISC-Z', 'c')            # tutorial — имя проекта,
                                  # c — язык программирования

if get_option('buildtype') == 'debug'
  add_project_arguments('-DDEBUG',  '-g3', '-std=c2x', language : 'c')
endif

if get_option('buildtype') == 'release'
  add_project_arguments('-DNDEBUG', '-O3', '-std=c2x', language : 'c')
endif

executable('risc-z', 'main.c', 'cpu.c', 'memory.c', 'ecall.c')
