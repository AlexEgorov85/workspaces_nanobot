import markdown
import re
import os
from bs4 import BeautifulSoup


def parse_markdown_to_slides(md_content):
    slides = []
    slide_blocks = re.split(r'^---$', md_content, flags=re.MULTILINE)

    for block in slide_blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split('\n')
        title = None
        content = []
        mermaid = None

        for line in lines:
            if line.startswith('# '):
                title = line[2:].strip()
                break

        mermaid_block = False
        mermaid_code = []
        for line in lines:
            if line.startswith('```mermaid'):
                mermaid_block = True
                continue
            elif line.startswith('```') and mermaid_block:
                mermaid_block = False
                mermaid = '\n'.join(mermaid_code)
                mermaid_code = []
                continue

            if mermaid_block:
                mermaid_code.append(line)
            elif not line.startswith('# '):
                content.append(line)

        slides.append({
            'title': title,
            'content': '\n'.join(content),
            'mermaid': mermaid
        })

    return slides


def generate_html(slides, template_path, css_path, output_path, title="Презентация"):
    with open(template_path, 'r', encoding='utf-8') as f:
        template_html = f.read()

    with open(css_path, 'r', encoding='utf-8') as f:
        css_content = f.read()

    for slide in slides:
        if slide['content']:
            slide['content'] = markdown.markdown(slide['content'], extensions=['tables'])

    soup = BeautifulSoup(template_html, 'html.parser')

    title_tag = soup.find('title')
    if title_tag:
        title_tag.string = title

    style_tag = soup.new_tag('style')
    style_tag.string = css_content
    soup.head.append(style_tag)

    container = soup.find(id='slides-container')
    if not container:
        container = soup.find('body')

    for slide in slides:
        slide_div = soup.new_tag('div', **{'class': 'slide'})
        content_div = soup.new_tag('div', **{'class': 'slide-content'})

        if slide['title']:
            h1 = soup.new_tag('h1')
            h1.string = slide['title']
            content_div.append(h1)

        if slide['content']:
            inner = soup.new_tag('div', **{'class': 'content'})
            inner.append(BeautifulSoup(slide['content'], 'html.parser'))
            content_div.append(inner)

        if slide['mermaid']:
            mermaid_div = soup.new_tag('div', **{'class': 'mermaid'})
            mermaid_div.string = slide['mermaid']
            content_div.append(mermaid_div)

        slide_div.append(content_div)
        container.append(slide_div)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(str(soup))

    return output_path
