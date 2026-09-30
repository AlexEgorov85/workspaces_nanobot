# repeat-guard-hook

OpenSpec change для введения `RepeatGuardHook` — общего runtime-защитника
от деградирующих циклов одинаковых tool-вызовов (по fingerprint'у
`tool_name + normalized args` в скользящем окне). Дефолт `off`,
обратная совместимость с существующими деплоями. Подробности —
`proposal.md` / `specs/runtime/anti-loop/spec.md` / `design.md`.
