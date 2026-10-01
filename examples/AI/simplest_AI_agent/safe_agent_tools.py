import math
import re

class SafeCalculator:
    def calculate(self, expression):
        """Вычисляет математическое выражение безопасно"""
        # Разрешаем только безопасные символы
        allowed_pattern = r'^[\d\s\+\-\*\/\(\)\.\,\%\^]+$'
        if not re.match(allowed_pattern, expression):
            return "Error: Invalid expression"

        try:
            # Заменяем ^ на ** для возведения в степень
            expression = expression.replace('^', '**')
            result = eval(expression, {"__builtins__": {}}, {})
            return str(result)
        except:
            return "Error: Calculation failed"

    def factorial(self, n):
        """Вычисляет факториал"""
        try:
            n = int(n)
            if n < 0 or n > 20:  # Ограничение для безопасности
                return "Error: Number must be between 0 and 20"
            return str(math.factorial(n))
        except:
            return "Error: Invalid input"

class VirtualFileSystem:
    def __init__(self):
        # Предзаданные файлы с содержимым
        self.files = {
            "README.md": "# My Project\nThis is a demo project.\nAuthor: Student",
            "main.py": "def hello():\n    print('Hello, world!')\n\nif __name__ == '__main__':\n    hello()",
            "config.json": '{"debug": true, "port": 8080, "version": "1.0.0"}',
            "data.txt": "Line 1\nLine 2\nLine 3\nTotal lines: 3",
            "requirements.txt": "flask==2.3.0\nrequests==2.31.0\nnumpy==1.24.0",
        }

    def list_files(self):
        """Возвращает список файлов"""
        return "\n".join(sorted(self.files.keys()))

    def read_file(self, filename):
        """Читает файл"""
        if filename in self.files:
            return self.files[filename]
        else:
            return f"Error: File '{filename}' not found"

    def file_exists(self, filename):
        """Проверяет существование файла"""
        return filename in self.files

class SafeCalculator:
    def calculate(self, expression):
        """Вычисляет математическое выражение безопасно"""
        # Разрешаем только безопасные символы
        allowed_pattern = r'^[\d\s\+\-\*\/\(\)\.\,\%\^]+$'
        if not re.match(allowed_pattern, expression):
            return "Error: Invalid expression"

        try:
            # Заменяем ^ на ** для возведения в степень
            expression = expression.replace('^', '**')
            result = eval(expression, {"__builtins__": {}}, {})
            return str(result)
        except:
            return "Error: Calculation failed"

    def factorial(self, n):
        """Вычисляет факториал"""
        try:
            n = int(n)
            if n < 0 or n > 20:  # Ограничение для безопасности
                return "Error: Number must be between 0 and 20"
            return str(math.factorial(n))
        except:
            return "Error: Invalid input"

class DocumentationSearch:
    def __init__(self):
        # Захардкоженная "документация"
        self.docs = {
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

    def search(self, query):
        """Ищет по документации"""
        query = query.lower().strip()

        # Прямое совпадение
        if query in self.docs:
            return f"Documentation for '{query}':\n{self.docs[query]}"

        # Поиск по подстроке
        results = []
        for key, doc in self.docs.items():
            if query in key or query in doc.lower():
                results.append(f"- {key}: {doc.split(chr(10))[0]}")

        if results:
            return f"Found {len(results)} results:\n" + "\n".join(results[:5])
        else:
            return f"No documentation found for '{query}'"
