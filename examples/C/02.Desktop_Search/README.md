Идея задания — сделать предметно-ориентированный текстовый поисковик для десктопа и специализированных веб-ресурсов. Поскольку это высокоуровневая задача, в основном связанная с автоматизацией использвоания других инструментов, используем Bash (можно sh/dash/ksh, увы только их поддерживает ShellCheck).

Параметры обрабатываем либо [вот так](https://stackoverflow.com/a/14203146), либо при помощи [`getopts`](https://man7.org/linux/man-pages/man1/getopts.1p.html), а ещё лучше [`getopt`](https://stackoverflow.com/a/7948533)

Работаем в каталоге  `02.Desktop_Search` и в ветке Git `02_DESKTOP_SEARCH`. По мере исполнения заданий делаем пуллреквесты в *основную* (`main` или `master`) ветку.