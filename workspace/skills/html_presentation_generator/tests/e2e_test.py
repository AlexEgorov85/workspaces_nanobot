# -*- coding: utf-8 -*-
"""Сквозные тесты html_presentation_generator: реальная генерация HTML."""
import sys, os, json

skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(skill_root, 'scripts'))  # [0] scripts/ — for `from tool`
sys.path.insert(1, skill_root)                            # [1] skill_root — for `import scripts`

passed = 0
failed = 0

def ok(msg):
    global passed; passed += 1
    print(f'  [OK] {msg}')

def fail(msg, detail=''):
    global failed; failed += 1
    print(f'  [FAIL] {msg}')
    if detail:
        print(f'     {detail}')

print('=== html_presentation_generator: E2E tests ===\n')

# ─────────────────────────────────────────────
# 1. Парсинг Markdown
# ─────────────────────────────────────────────
print('[1] parse_markdown_to_slides')
from generator import parse_markdown_to_slides

md = (
    '# Title Slide\n\n'
    'Subtitle\n\n'
    '---\n\n'
    '# Slide 2\n\n'
    '**bold** text\n\n'
    '- List item\n\n'
    '| Col1 | Col2 |\n'
    '|------|------|\n'
    '| A    | B    |\n\n'
    '```mermaid\n'
    'pie title Test\n'
    '"Cat A" : 40\n'
    '"Cat B" : 60\n'
    '```\n\n'
    '---\n\n'
    '# Slide 3\n\n'
    'Final slide.'
)

slides = parse_markdown_to_slides(md)
assert len(slides) == 3, f'Expected 3, got {len(slides)}'
assert slides[0]['title'] == 'Title Slide'
assert slides[1]['title'] == 'Slide 2'
assert slides[1]['mermaid'] is not None
assert 'Cat A' in slides[1]['mermaid']
assert slides[2]['title'] == 'Slide 3'
ok(f'3 slides, mermaid, tables, lists')

# ─────────────────────────────────────────────
# 2. Генерация HTML
# ─────────────────────────────────────────────
print('\n[2] generate_html')
from generator import generate_html

template = os.path.join(skill_root, 'scripts', 'templates', 'base.html')
css = os.path.join(skill_root, 'scripts', 'assets', 'styles.css')
out = os.path.join(os.environ.get('TEMP', '/tmp'), 'e2e_test_output.html')

result = generate_html(slides, template, css, out, title='E2E Test')
assert result == out
assert os.path.isfile(out)

with open(out, 'r', encoding='utf-8') as f:
    html = f.read()

from bs4 import BeautifulSoup
soup = BeautifulSoup(html, 'html.parser')

assert soup.find('title').string == 'E2E Test'
assert len(soup.find_all('div', class_='slide')) == 3
assert len(soup.find_all('div', class_='slide-content')) == 3
assert len(soup.find_all('div', class_='mermaid')) == 1
assert 'mermaid.min.js' in html
assert soup.find(id='prev-btn') is not None
assert soup.find(id='next-btn') is not None
assert soup.find(id='counter') is not None

size = os.path.getsize(out)
ok(f'3 slides, {size} bytes, CSS+JS+nav embedded')

os.remove(out)

# ─────────────────────────────────────────────
# 3. CLI import
# ─────────────────────────────────────────────
print('\n[3] cli.py')
import cli
assert callable(cli.main)
ok('import + callable')

# ─────────────────────────────────────────────
# 4. __init__ public API
# ─────────────────────────────────────────────
print('\n[4] scripts/__init__.py')
import scripts
assert callable(scripts.parse_markdown_to_slides)
assert callable(scripts.generate_html)
assert hasattr(scripts, 'HtmlPresentationTool')
ok('public API functions available')

# ─────────────────────────────────────────────
# 5. Root generate_presentation()
# ─────────────────────────────────────────────
print('\n[5] root generate_presentation()')
sys.path.insert(0, os.path.dirname(skill_root))  # parent dir for `import html_presentation_generator`
from html_presentation_generator import generate_presentation

path = generate_presentation('# Title\n\n---\n\n# Slide 2\nContent', title='Root API Test')
assert os.path.isfile(path)
assert path.endswith('.html')
with open(path, 'r', encoding='utf-8') as f:
    h = f.read()
assert 'Root API Test' in h
assert 'Title' in h
os.remove(path)
ok('generate_presentation() generates valid HTML')

# ─────────────────────────────────────────────
print(f'\n{"="*50}')
print(f'ИТОГО: {passed} [OK], {failed} [FAIL]')
sys.exit(0 if failed == 0 else 1)
