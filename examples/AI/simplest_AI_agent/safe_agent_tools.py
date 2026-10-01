"""
Инструменты агента — три независимых класса.

Агент (simple_ai_agent.py) занимается только диалогом с моделью,
а всё "умение" живёт здесь. Каждый инструмент — обычный класс Python;
агент вызывает его методы и показывает модели результат.

Два принципа, на которых держится эта архитектура:

  1. Инструменты изолированы. Калькулятор ничего не знает о файлах,
     поиск по документации — о калькуляторе. Каждый класс можно
     заменить или выкинуть, не трогая остальные.
  2. Инструмент возвращает строку (str) — ровно то, что увидит модель.
     Поэтому ошибки ловятся и превращаются в текст ("Error: ..."),
     а не в исключения: так модель может прочитать ошибку и поправиться.
"""

import math
import re


class SafeCalculator:
    """Калькулятор: арифметика и факториал."""

    # Белый список символов: только цифры и арифметические операторы.
    # Знак "_" сюда не входит — поэтому добраться до __class__ или
    # __import__ через eval нельзя. Вместе с пустым __builtins__
    # в evaluate() это делает вычисления безопасными (для демки — более чем).
    ALLOWED = re.compile(r"^[\d\s\+\-\*\/\(\)\.\,\%\^]+$")

    def calculate(self, expression: str) -> str:
        """Вычисляет арифметическое выражение, например "2 + 2" или "10 * 5"."""
        if not self.ALLOWED.match(expression):
            return "Error: Invalid expression"

        try:
            # "^" в математике — это степень, а в Python — XOR.
            expression = expression.replace("^", "**")
            # Пустой __builtins__ означает, что в области видимости нет
            # ни одной встроенной функции — вызвать их нельзя.
            result = eval(expression, {"__builtins__": {}}, {})
            return str(result)
        except Exception:  # noqa: BLE001 — список ошибок eval заранее неизвестен
            # Сюда попадают ZeroDivisionError, NameError и т.п.
            # Ловим Exception, а не голый except: тот заодно перехватывает
            # Ctrl-C, а это не ошибка калькулятора.
            return "Error: Calculation failed"

    def factorial(self, n: int | str) -> str:
        """Факториал целого числа. Принимает и int, и строку из JSON."""
        try:
            n = int(n)
            if n < 0 or n > 20:  # Ограничение для безопасности
                return "Error: Number must be between 0 and 20"
            return str(math.factorial(n))
        except (TypeError, ValueError):
            return "Error: Invalid input"


class VirtualFileSystem:
    """Виртуальная файловая система: несколько файлов "в памяти".

    Настоящих файлов на диске нет — всё лежит в словаре files.
    Так агент ничего не ломает в реальной системе.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {
            "README.md": "# My Project\nThis is a demo project.\nAuthor: Student",
            "main.py": "def hello():\n    print('Hello, world!')\n\nif __name__ == '__main__':\n    hello()",
            "config.json": '{"debug": true, "port": 8080, "version": "1.0.0"}',
            "data.txt": "Line 1\nLine 2\nLine 3\nTotal lines: 3",
            "requirements.txt": "flask==2.3.0\nrequests==2.31.0\nnumpy==1.24.0",
        }

    def list_files(self) -> str:
        """Возвращает имена файлов, каждый на новой строке."""
        return "\n".join(sorted(self.files.keys()))

    def read_file(self, filename: str) -> str:
        """Возвращает содержимое файла или сообщение об ошибке."""
        if filename in self.files:
            return self.files[filename]
        return f"Error: File '{filename}' not found"

    def file_exists(self, filename: str) -> bool:
        """Есть ли такой файл (True / False)."""
        return filename in self.files


class DocumentationSearch:
    """Поиск по маленькому захардкоженному справочнику Python."""

    def __init__(self) -> None:
        self.docs: dict[str, str] = {
            "print": "print(*objects, sep=' ', end='\\n', file=sys.stdout, flush=False)\n\nPrints the values to a stream, or to sys.stdout by default.",
            "len": "len(obj)\n\nReturn the number of items in a container.",
            "range": "range(stop)\nrange(start, stop[, step])\n\nReturn an immutable sequence type.",
            "open": "open(file, mode='r', buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None)\n\nOpen file and return a stream.",
            "str.split": "str.split(sep=None, maxsplit=-1)\n\nReturn a list of the words in the string, using sep as the delimiter string.",
            "list.append": "list.append(object)\n\nAppend object to the end of the list.",
            "dict.get": "dict.get(key, default=None)\n\nReturn the value for key if key is in the dictionary, else default.",
            "sorted": "sorted(iterable, *, key=None, reverse=False)\n\nReturn a new sorted list from the items in iterable.",
            "enumerate": "enumerate(iterable, start=0)\n\nReturn an enumerate object.",
            "zip": "zip(*iterables)\n\nIterate over several iterables in parallel.",
        }

    def search(self, query: str) -> str:
        """Ищет по справочнику. Сначала точное совпадение, потом по подстроке."""
        query = query.lower().strip()

        # Точное совпадение — самый частый и самый точный случай.
        if query in self.docs:
            return f"Documentation for '{query}':\n{self.docs[query]}"

        # Иначе ищем по подстроке: в ключе или прямо в тексте.
        results: list[str] = []
        for key, doc in self.docs.items():
            if query in key or query in doc.lower():
                first_line = doc.split("\n")[0]
                results.append(f"- {key}: {first_line}")

        if results:
            return f"Found {len(results)} results:\n" + "\n".join(results[:5])
        return f"No documentation found for '{query}'"
