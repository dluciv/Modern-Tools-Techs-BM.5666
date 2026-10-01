"""
Модуль цепи Маркова: обучение и ответы на вопросы.

Здесь живёт весь «мозг» модели и ничего кроме него — ни Flask, ни HTTP.
Сервер из markov_slm.py только перекладывает сюда текст запроса
и забирает готовую строку ответа.

Главная идея: состояние цепи — это основы последних `order` слов,
а хранит цепь исходные формы слов. Поэтому на выходе мы получаем
обычные русские слова с правильными окончаниями, а совпадения
находятся в разы чаще.
"""

import random
from collections import Counter, defaultdict

from slm_corpus import (
    detokenize,
    ends_sentence,
    stem,
    stem_tokens,
    tokenize,
    word_form,
)

# Порядок цепи. При order=4 и корпусе в ~11 тысяч токенов почти каждая
# 4-грамма встречалась ровно один раз, и ввод почти никогда не попадал
# в цепь. Двухсловное окно попадает в разы чаще, и в цепи появляется
# реальный выбор продолжения.
DEFAULT_ORDER = 2

# Температура по умолчанию — как у настоящих моделей OpenAI.
#
# Сразу стоит знать про неё главное: на этой цепи параметр почти
# ничего не делает. Температура перераспределяет веса ВНУТРИ одного
# состояния, а 93% состояний тут имеют единственный переход — softmax
# от одного элемента равен 1.0 при любой температуре. Реальный выбор
# есть примерно в каждом восьмом токене ответа, остальное предопределено
# корпусом. Это хорошая иллюстрация к «слабая модель обесценивает
# сильный агент»: самый ходовой параметр у LLM тут почти бесполезен.
DEFAULT_TEMPERATURE = 1.0

# На это количество состояний цепь отвечает отказом
NO_INFO_ANSWER = "I don't have enough information to answer that."


class ImprovedMarkovChain:
    """Цепь Маркова: модель, которая помнит только последние order слов.

    Хранит две вещи:
      * self.chain     — состояние (кортеж основ) -> список следующих слов
      * self.start_states — состояния, которыми текст начинается
    """

    def __init__(
        self,
        training_texts: list[str],
        order: int = DEFAULT_ORDER,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        self.order = order
        self.temperature = temperature
        self.chain: defaultdict[tuple[str, ...], list[str]] = defaultdict(list)
        self.start_states: list[tuple[str, ...]] = []

        # Заготовки ответов для типовых запросов агентов (tool-calling, MCP и т.п.)
        # Агенты часто отправляют длинные системные промпты с упоминанием
        # этих слов, и без заготовок агент получает вместо ответа абракадабру.
        self.agent_responses: dict[str, str] = {
            "list files": "Here are the files in the current directory: README.md, main.py, config.json, data.txt, requirements.txt",
            "list_files": "Here are the files in the current directory: README.md, main.py, config.json, data.txt, requirements.txt",
            "read file": "File contents: # My Project\nThis is a demo project.\nAuthor: Student",
            "read_file": "File contents: # My Project\nThis is a demo project.\nAuthor: Student",
            "calculate": "The result is: 42",
            "search": "Found 3 results matching your query",
            "tool": "I'm sorry, I cannot use tools yet. My brain is still training.",
            "mcp": "Model Context Protocol is a standard for connecting AI models to tools.",
        }

        for text in training_texts:
            self.train_text(text)

    # ------------------------------------------------------------------
    # Обучение
    # ------------------------------------------------------------------

    def train_text(self, text: str) -> None:
        """Обучает цепь на одном тексте."""
        tokens = tokenize(text)
        stems = stem_tokens(tokens)

        for i in range(len(tokens) - self.order):
            state = tuple(stems[i : i + self.order])
            next_token = tokens[i + self.order]
            self.chain[state].append(next_token)

            # Начало предложения: либо самое начало текста, либо предыдущий
            # токен закончился точкой/!/? — теперь эти знаки внутри токена,
            # поэтому проверяем последний символ
            if i == 0 or ends_sentence(tokens[i - 1]):
                self.start_states.append(state)

    # ------------------------------------------------------------------
    # Генерация
    # ------------------------------------------------------------------

    def generate(
        self, prompt: str, max_tokens: int = 50, temperature: float | None = None
    ) -> str:
        """Продолжает промпт."""
        # Сначала проверяем заготовки для агентов
        prompt_lower = prompt.lower()
        for key, response in self.agent_responses.items():
            if key in prompt_lower:
                return response

        tokens = tokenize(prompt)

        if len(tokens) < self.order:
            # Ввод короче окна цепи — продолжать нечего, берём любое
            # начало предложения из корпуса
            if not self.start_states:
                return "I'm not sure how to respond to that."
            state = random.choice(self.start_states)
        else:
            # Ключи цепи — основы, поэтому стеммим и ввод тоже
            state = tuple(stem_tokens(tokens[-self.order :]))

        generated = self.generate_from_state(state, max_tokens, temperature)

        return generated if generated else NO_INFO_ANSWER

    def generate_from_state(
        self,
        state: tuple[str, ...],
        max_tokens: int = 20,
        temperature: float | None = None,
    ) -> str:
        """Генерирует текст, начав с указанного состояния цепи."""
        generated: list[str] = []
        current_state = state

        for _ in range(max_tokens):
            if current_state not in self.chain:
                possible_states = self.find_similar_states(current_state)
                if not possible_states:
                    break
                current_state = random.choice(possible_states)

            next_token = self.weighted_choice(self.chain[current_state], temperature)
            generated.append(next_token)

            # Предложение закончилось — знак препинания теперь внутри токена
            if ends_sentence(next_token):
                break

            # Сдвигаем окно и переводим новое слово в основу:
            # ключом цепи должна быть основа, а в ответ пойдёт само слово
            current_state = current_state[1:] + (stem(word_form(next_token)),)

        return detokenize(generated)

    def find_similar_states(self, state: tuple[str, ...]) -> list[tuple[str, ...]]:
        """Находим состояния, похожие на данное, и оставляем только лучшие.

        Порог входа — хотя бы один совпавший токен из order. При order=4
        это было «3 из 4» (строгий отбор), а при order=2 «хотя бы один из
        двух» — и это уже очень широко: в цепь попадает всё, что делит одно
        слово с вводом.

        Но отдаём мы не всех, а только те, у кого совпадений больше
        всего. Иначе выбор случайного состояния цеплял бы к вводу
        одно случайное слово и уводил в любую сторону.
        """
        scored: list[tuple[int, tuple[str, ...]]] = []
        for candidate in self.chain:
            matches = sum(1 for a, b in zip(candidate, state) if a == b)
            if matches >= max(1, self.order - 1):
                scored.append((matches, candidate))

        if not scored:
            return []

        best = max(matches for matches, _ in scored)
        return [candidate for matches, candidate in scored if matches == best]

    def weighted_choice(self, tokens: list[str], temperature: float | None = None) -> str:
        """Выбирает следующий токен, взвешивая его частоту с поправкой на температуру.

        Это ровно тот же приём, что и у настоящих моделей: у LLM вероятность
        каждого следующего слова считается как softmax(logit / T), и чем
        больше T, тем площе распределение. Только вместо логитов у нас
        счётчики из корпуса, поэтому вес равен count ** (1 / T).

        Отсюда два приятных отличия от softmax: при T -> 0 веса не
        переполняются (это счётчики, а не произвольные числа), а при
        T -> бесконечности все веса стремятся к единице и выбор
        становится равномерным — тоже без взрыва.
        """
        if not tokens:
            return ""

        temp = self.temperature if temperature is None else temperature
        counts = Counter(tokens)

        if temp <= 0:
            # T = 0 — жадный выбор: всегда самое частое продолжение
            return counts.most_common(1)[0][0]

        weights = [count ** (1.0 / temp) for count in counts.values()]

        return random.choices(list(counts.keys()), weights=weights)[0]

    # ------------------------------------------------------------------
    # Ответы на вопросы
    # ------------------------------------------------------------------

    def respond(
        self, message: str, max_tokens: int = 50, temperature: float | None = None
    ) -> str:
        """Отвечает на реплику пользователя.

        Простейшая «логика диалога»: приветствия и прощания получают
        заготовленный ответ, вопрос со знаком «?» ищет похожее состояние,
        всё остальное просто продолжается цепью.
        """
        lowered = message.lower()

        if any(word in lowered for word in ("привет", "здравствуй", "добрый", "хай")):
            return self.generate("Привет! Как дела?", max_tokens=max_tokens, temperature=temperature)

        if "?" in message:
            return self.answer_question(message, temperature=temperature)

        if any(word in lowered for word in ("пока", "до свидания", "прощай")):
            return self.generate("До свидания! Удачи тебе.", max_tokens=max_tokens, temperature=temperature)

        return self.generate(message, max_tokens=max_tokens, temperature=temperature)

    def answer_question(self, question: str, temperature: float | None = None) -> str:
        """Отвечает на вопрос, подбирая состояние с наибольшим общим словом.

        Это сознательно примитивный «поиск по смыслу»: мы не понимаем
        вопрос, мы просто ищем в цепи состояние, которое делит с ним
        максимум основ. Именно поэтому модель отвечает на тему, а не
        по существу.
        """
        question_stems = set(stem_tokens(tokenize(question)))
        if not question_stems:
            return self.generate("", max_tokens=15)

        best_score = 0
        best_matches: list[tuple[str, ...]] = []

        for state in self.chain:
            score = len(set(state) & question_stems)
            if score > best_score:
                best_score = score
                best_matches = [state]
            elif score == best_score:
                # Набирается нередко: вопрос почти всегда делит слово
                # с кучей состояний (например, служебное «что»).
                # Копить всех и брать случайного — иначе модель отвечает
                # на один и тот же вопрос одной и той же фразой
                best_matches.append(state)

        if best_score >= 1:
            return self.generate_from_state(
                random.choice(best_matches), max_tokens=20, temperature=temperature
            )

        return self.generate("", max_tokens=15)