cfsmcp2 — Windows portable
============================

1. Распакуйте ZIP в любую папку (например D:\tools\cfsmcp2).
2. Запустите cfsmcp2.exe (единственный exe в корне; сервер — тот же exe в режиме --server).
3. Нажмите «Старт», затем «Открыть UI» (по умолчанию http://127.0.0.1:8561/).
4. MCP в Cursor: скопируйте URL из поля MCP в окне launcher (по умолчанию http://127.0.0.1:8561/mcp/).

Каталог data/ (SQLite, индексы, загрузки) создаётся рядом с exe.
Доступ (локально / по сети) и порт — в окне cfsmcp2.exe; сохраняются в cfsmcp2.ini.

LM Studio (семантический поиск): локальный сервер на порту 1234.
Без LM Studio работают FTS и карточки объектов.

Источники 1С: укажите реальный путь на диске (import-path / «Локальный путь»).

Обновление
----------
Автоматически: щелчок по бейджу версии (v…) в окне launcher → проверка GitHub →
скачивание, установка, перезапуск (data/ и cfsmcp2.ini сохраняются).

Вручную:
1. Скачайте новый ZIP с GitHub Releases.
2. Остановите сервер в окне launcher.
3. Распакуйте поверх установки, не заменяя data/ и cfsmcp2.ini.

Сборка из исходников (разработчик)
----------------------------------
  powershell -ExecutionPolicy Bypass -File packaging\build-portable.ps1

Результат: dist\cfsmcp2-win-portable\

ZIP для GitHub Release
----------------------
  powershell -ExecutionPolicy Bypass -File packaging\package-portable.ps1

Результат: dist\cfsmcp2-win-portable-v<версия>.zip

Перед git push (один раз)
-------------------------
  powershell -ExecutionPolicy Bypass -File packaging\install-hooks.ps1
  gh auth login

При push в master/main автоматически:
  1. сборка portable + ZIP (pre-push)
  2. GitHub Release v<версия> с ZIP (после push, нужен gh auth login)

Пропустить: SKIP_PORTABLE_BUILD=1 и/или SKIP_GITHUB_RELEASE=1
