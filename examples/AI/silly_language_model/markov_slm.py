#!/usr/bin/env -S uv run --quiet --script --
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "flask>=3.1.3",
# ]
# ///
"""
Silly Language Model — с поддержкой стриминга и заготовками для агентов
"""

from glob import glob
import random
import re
import json as json_module
from collections import defaultdict, Counter
from flask import Flask, request, jsonify, Response
import time
import uuid

app = Flask(__name__)

class ImprovedMarkovChain:
    def __init__(self, training_texts, order=4):
        self.order = order
        self.chain = defaultdict(list)
        self.start_states = []

        # Заготовки ответов для типовых запросов агентов (tool-calling, MCP и т.п.)
        # Агенты часто отправляют длинные системные промпты с упоминанием этих слов
        self.agent_responses = {
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

    def tokenize(self, text):
        """Улучшенная токенизация для русского языка"""
        text = re.sub(r'([,.!?;:])', r' \1 ', text)
        tokens = re.findall(r'\b\w+\b|[^\w\s]', text.lower())
        return tokens

    def train_text(self, text):
        """Обучение на одном тексте"""
        tokens = self.tokenize(text)

        for i in range(len(tokens) - self.order):
            state = tuple(tokens[i:i+self.order])
            next_token = tokens[i + self.order]
            self.chain[state].append(next_token)

            if i == 0 or tokens[i-1] in '.!?':
                self.start_states.append(state)

    def generate(self, prompt, max_tokens=50):
        """Генерация продолжения промпта"""
        # Сначала проверяем заготовки для агентов
        prompt_lower = prompt.lower()
        for key, response in self.agent_responses.items():
            if key in prompt_lower:
                return response

        tokens = self.tokenize(prompt)

        if len(tokens) < self.order:
            if self.start_states:
                state = random.choice(self.start_states)
                tokens = list(state)
            else:
                return "I'm not sure how to respond to that."
        else:
            state = tuple(tokens[-self.order:])

        generated = []
        current_state = state

        for _ in range(max_tokens):
            if current_state not in self.chain:
                possible_states = self.find_similar_states(current_state)
                if not possible_states:
                    break
                current_state = random.choice(possible_states)

            next_token = self.weighted_choice(self.chain[current_state])
            generated.append(next_token)

            if next_token in '.!?':
                break

            current_state = current_state[1:] + (next_token,)

        result = self.detokenize(generated)
        return result if result else "I don't have enough information to answer that."

    def find_similar_states(self, state):
        """Находим состояния, похожие на данное"""
        similar = []
        for s in self.chain.keys():
            matches = sum(1 for a, b in zip(s, state) if a == b)
            if matches >= self.order - 1:
                similar.append(s)
        return similar

    def weighted_choice(self, tokens):
        """Взвешенный выбор токена (частые токены выбираются чаще)"""
        if not tokens:
            return ""

        token_counts = Counter(tokens)

        weighted_tokens = []
        weights = []
        for token, count in token_counts.items():
            weighted_tokens.append(token)
            weights.append(count)

        return random.choices(weighted_tokens, weights=weights)[0]

    def detokenize(self, tokens):
        """Собираем токены обратно в текст"""
        if not tokens:
            return ""

        result = tokens[0]
        for token in tokens[1:]:
            if token in '.,!?;:':
                result += token
            else:
                result += ' ' + token

        if result and result[0].isalpha():
            result = result[0].upper() + result[1:]

        return result

    def answer_question(self, question):
        """Пытаемся ответить на вопрос, используя корпус"""
        question_tokens = self.tokenize(question)
        # Множество считаем один раз, а не внутри цикла: иначе на каждом
        # из ~11 тысяч состояний пересобираем его заново
        question_words = set(question_tokens)

        best_match = None
        best_score = 0

        for state in self.chain.keys():
            overlap = len(set(state) & question_words)

            if overlap > best_score:
                best_score = overlap
                best_match = state

        if best_match and best_score >= 1:
            return self.generate_from_state(best_match, max_tokens=20)

        return self.generate("", max_tokens=15)

    def generate_from_state(self, state, max_tokens=20):
        """Генерация из конкретного состояния"""
        generated = []
        current_state = state

        for _ in range(max_tokens):
            if current_state not in self.chain:
                break

            next_token = self.weighted_choice(self.chain[current_state])
            generated.append(next_token)

            if next_token in '.!?':
                break

            current_state = current_state[1:] + (next_token,)

        return self.detokenize(generated)

# Обучающий корпус на русском
training_data = [
    "Привет! Как дела? Я рада тебя видеть.",
    "Здравствуй, дорогой друг! Как настроение сегодня?",
    "Добрый день! Чем могу помочь?",
    "Привет! Рад тебя видеть. Как прошла неделя?",

    "Искусственный интеллект — это область информатики, которая занимается созданием умных машин.",
    "Машинное обучение позволяет компьютерам учиться на данных без явного программирования.",
    "Нейронные сети имитируют работу человеческого мозга для решения сложных задач.",
    "Большие языковые модели способны генерировать текст, похожий на человеческий.",

    "Программирование — это процесс создания компьютерных программ.",
    "Хороший код должен быть читаемым, эффективным и надежным.",
    "Тестирование помогает находить ошибки в программах до их выпуска.",
    "Версионирование кода позволяет отслеживать изменения и работать в команде.",

    "Жизнь прекрасна и удивительна! Каждый день приносит что-то новое.",
    "Учиться никогда не поздно. Знания открывают новые возможности.",
    "Чтение книг расширяет кругозор и развивает мышление.",
    "Музыка способна поднять настроение и вдохновить на новые свершения.",

    "Погода сегодня отличная! Солнце светит, птицы поют.",
    "Лето — прекрасное время для отдыха и путешествий.",
    "Осень приносит яркие краски и прохладу.",
    "Зима — время снега, лыж и горячего чая.",

    "Технологии развиваются очень быстро. То, что казалось фантастикой вчера, сегодня становится реальностью.",
    "Интернет изменил способы общения и получения информации.",
    "Смартфоны стали неотъемлемой частью нашей жизни.",
    "Робототехника открывает новые возможности для автоматизации.",

    "Наука помогает нам понимать мир вокруг нас.",
    "Физика изучает законы природы и свойства материи.",
    "Математика — язык, на котором написана вселенная.",
    "Биология раскрывает тайны живых организмов.",

    "Спасибо за интересный разговор! Было приятно пообщаться.",
    "До свидания! Удачи тебе во всех начинаниях.",
    "Хорошего дня! Пусть все получится.",
    "Пока! Не забывай заходить в гости.",
]

paths = glob("markov*.txt")

for path in paths:
    with open(path, "r", encoding="utf-8") as f:
        training_data.append(f.read())

model = ImprovedMarkovChain(training_data, order=4)


def generate_streaming_response(request_id, created_time, model_name, content,
                                prompt_tokens=0, completion_tokens=0):
    """Генератор для стримингового ответа в формате SSE (Server-Sent Events)"""
    # Первый чанк по спецификации OpenAI несёт только роль,
    # без символов ответа — так клиент знает, что начался поток
    def chunk(delta, finish_reason=None):
        return {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created_time,
            "model": model_name,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason
            }]
        }

    yield f"data: {json_module.dumps(chunk({'role': 'assistant', 'content': ''}), ensure_ascii=False)}\n\n"

    # Разбиваем на слова — так стриминг выглядит естественнее
    words = content.split()

    for i, word in enumerate(words):
        token = word if i == 0 else " " + word

        yield f"data: {json_module.dumps(chunk({'content': token}), ensure_ascii=False)}\n\n"
        time.sleep(0.03)  # имитация "раздумий" модели

    # Финальный чанк с finish_reason — это критично для агентов!
    final_chunk = chunk({}, "stop")
    # usage по спеке приходит в последнем чанке потока, иначе клиент
    # (в том числе OpenCode) показывает нули вместо расхода токенов
    final_chunk["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

    yield f"data: {json_module.dumps(final_chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    # request.json падает с 400, если тело битое или пустое, и Flask
    # отдаёт HTML-страницу. Клиент OpenAI ждёт JSON с полем error,
    # поэтому аккуратно ловим оба случая.
    try:
        data = request.get_json(force=False, silent=True)
    except Exception:
        data = None

    if not isinstance(data, dict):
        return jsonify({
            "error": {
                "message": "Expected a JSON object in the request body",
                "type": "invalid_request_error",
                "code": None,
            }
        }), 400

    messages = data.get('messages') or []
    if not isinstance(messages, list):
        return jsonify({
            "error": {
                "message": "'messages' must be a list",
                "type": "invalid_request_error",
                "code": None,
            }
        }), 400

    stream = data.get('stream', False)  # Поддержка стриминга
    user_message = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    max_tokens = data.get('max_tokens') or 100
    # Приводим к целому: клиент может прислать строку, а range() её не берёт
    try:
        max_tokens = max(1, int(max_tokens))
    except (TypeError, ValueError):
        max_tokens = 100

    # Простая диалоговая логика (сохраняем вашу)
    user_lower = user_message.lower()

    if any(word in user_lower for word in ['привет', 'здравствуй', 'добрый', 'хай']):
        response_text = model.generate("Привет! Как дела?", max_tokens=max_tokens)
    elif '?' in user_message:
        response_text = model.answer_question(user_message)
    elif any(word in user_lower for word in ['пока', 'до свидания', 'прощай']):
        response_text = model.generate("До свидания! Удачи тебе.", max_tokens=max_tokens)
    else:
        response_text = model.generate(user_message, max_tokens=max_tokens)

    request_id = f"chatcmpl-{uuid.uuid4()}"
    created_time = int(time.time())
    model_name = "markov-chain-v1"

    # Считаем токены так же, как в обычном ответе, — иначе в потоке
    # расход будет нулевым
    prompt_tokens = sum(len(m['content'].split()) for m in messages
                       if isinstance(m.get('content'), str))
    completion_tokens = len(response_text.split())

    # Стриминговый ответ
    if stream:
        return Response(
            generate_streaming_response(request_id, created_time, model_name, response_text,
                                        prompt_tokens, completion_tokens),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'  # для nginx
            }
        )

    # Обычный ответ
    return jsonify({
        "id": request_id,
        "object": "chat.completion",
        "created": created_time,
        "model": model_name,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text
            },
            "finish_reason": "stop"  # было "length" — это ломает агентов
        }],
        "usage": {
            "prompt_tokens": len(user_message.split()) if user_message else 0,
            "completion_tokens": len(response_text.split()),
            "total_tokens": (len(user_message.split()) if user_message else 0) + len(response_text.split())
        }
    })


@app.route('/v1/models', methods=['GET'])
def list_models():
    return jsonify({
        "object": "list",
        "data": [{
            "id": "markov-chain-v1",
            "object": "model",
            "created": int(time.time()),
            "owned_by": "student-demo"
        }]
    })


if __name__ == '__main__':
    # debug=True поднимает интерактивный отладчик, доступный любому,
    # кто дотянулся до порта. Для локальной демки это лишний риск:
    # включайте только на своей машине и только на время отладки.
    app.run(port=8000, debug=False)
