# Консольный шаблон саммарайзера контекста для русских диалогов.
# Переопределяет nanobot/templates/agent/consolidator_archive.md, подложенный
# через lib/services/consolidator_locale.py при старте приложения
# (ApplicationContext.start(); monkeypatch prompt_templates._environment —
# ChoiceLoader с приоритетом workspace/overrides/).
# Это единственный источник инструкции для Consolidator — nanobot не
# поддерживает явный override (см. default_dream_pattern).

Extract key facts from this conversation. For each fact, annotate its memory attributes.

Only SNIP facts deserve a non-[skip] mark:
- Signal: would the user need to repeat this if forgotten?
- Novel: not just a restatement of another fact in this same conversation chunk
- Important: prevents rework or captures preferences / rules
- Persistent: still relevant after 2 weeks

Output one fact per line in this format:
- [mark] fact content

Marks (choose the best match):
- [permanent] Core preferences, personal traits, habits — never becomes stale
- [durable] Technical discoveries, project knowledge, config details — valid for months
- [ephemeral] Active task state, temporary decisions — may change in weeks
- [correction] Correction to a previous memory — state what changed
- [skip] Does not meet SNIP criteria, is conversational filler, is code/source facts derivable from the repo, or is only useful as an audit breadcrumb

Priority: user corrections and preferences > solutions > decisions > events > environment facts. The most valuable memory prevents the user from having to repeat themselves.

Do not mark something [skip] merely because it might already exist in long-term memory; Dream handles cross-file deduplication later.

**IMPORTANT LANGUAGE RULE: Write all [permanent], [durable], and [ephemeral] facts in the SAME language as the ongoing conversation. If the dialogue is primarily in Russian — write the summary in Russian. Never switch to English unless explicitly requested.**
Output concise bullet points only. No preamble, no commentary.
If nothing noteworthy happened, output: (nothing)
