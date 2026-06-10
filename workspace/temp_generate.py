import sys, os
sys.path.append('skills/html_presentation_generator/scripts')
from generator import parse_markdown_to_slides, generate_html
md = '''# Мои навыки

---

## Навыки
- **folder‑analyzer** – анализ файлов и папок с Pandas.
- **stage‑driven‑development** – разбивка проектов на стадии.
- **structured‑report‑generation** – генерация Markdown/JSON‑отчётов.
- **windows‑cyrillic‑handling** – работа с кириллическими путями в Windows.
- **nanobot‑exec‑integration** – запуск скриптов через `exec`.
- **map‑reduce‑file‑analysis** – параллельный анализ больших файлов.
- **html‑presentation‑generator** – генерация HTML‑презентаций из Markdown.
- **data‑analyzer** – анализ CSV/Excel с графиками.
- **audit‑analyzer** – работа с аудиторскими данными.
- **clawhub** – поиск и установка навыков.
- **long‑goal** – управление длительными задачами.
- **skill‑creator** – создание и обновление навыков.

---

## Самооценка

| Навык | Оценка (1‑10) | Комментарий |
|-------|---------------|-------------|
| folder‑analyzer | 9 | Быстрый и надёжный, но иногда требует ручной настройки путей.
| stage‑driven‑development | 8 | Отлично структурирует работу, но иногда нужно уточнять параметры.
| structured‑report‑generation | 9 | Генерирует отчёты быстро, но формат иногда не совпадает с требованиями.
| windows‑cyrillic‑handling | 10 | Полностью решает проблемы с кодировкой.
| nanobot‑exec‑integration | 9 | Удобно, но требует правильных путей.
| map‑reduce‑file‑analysis | 8 | Хорошо масштабируется, но иногда медленнее из‑за сериализации.
| html‑presentation‑generator | 10 | Самодостаточный, легко делиться.
| data‑analyzer | 9 | Быстрый, но иногда нужно уточнять типы данных.
| audit‑analyzer | 8 | Полезен, но требует актуальной БД.
| clawhub | 7 | Быстро находит навыки, но иногда не обновляется.
| long‑goal | 9 | Позволяет держать фокус, но иногда нужно уточнять цели.
| skill‑creator | 8 | Упрощает создание навыков, но иногда нужно вручную поправлять шаблоны.

---

## Заключение

Я считаю, что мои навыки покрывают широкий спектр задач: от анализа данных до автоматизации и генерации презентаций. В целом, я оцениваю себя на 9/10 по всем критериям, но всегда есть место для улучшения, особенно в настройке параметров и интеграции с внешними системами.
'''
slides = parse_markdown_to_slides(md)
output_path='nanobot_skills_presentation.html'
generate_html(slides, 'skills/html_presentation_generator/scripts/templates/base.html', 'skills/html_presentation_generator/scripts/assets/styles.css', output_path, title='Мои навыки и оценка')
print('done')