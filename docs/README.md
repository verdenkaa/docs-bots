# Документация: локальный запуск и Vercel

Весь сайт находится в `docs/`. Остальной репозиторий свободен для основного проекта.
Исходные страницы — `docs/documents/`, конфигурация сайта — `docs/docusaurus.config.js`.
Этот README служит инструкцией настройки и не публикуется как страница сайта.

Каждая папка первого уровня внутри `documents` автоматически становится разделом
в шапке со своим боковым меню. Обязательный `_category_.yml` задаёт `label` и `position`.
Вложенные папки становятся категориями бокового меню. Примеры: `documents/drafts/`
и `documents/contracts/` с документом-заглушкой. Правила и примеры YAML — в `AGENTS.md`.
Папки без документов скрываются из шапки; вкладка ведёт к первому документу раздела.
YAML front matter между `---` в начале страницы задаёт её заголовок, URL (`slug`)
и порядок (`sidebar_position`). После добавления раздела или изменения YAML
перезапустите локальный сервер.
Конфигурация Docusaurus и Vercel остаётся в корне `docs`, вне публикуемого контента.

По умолчанию используется тёмная тема. Если браузер уже сохранил светлую тему,
переключите её кнопкой в шапке: ручной выбор сохраняется.
Локальный поиск по русским и английским текстам находится в шапке (Ctrl+K / Cmd+K).
Индекс создаётся при `npm run build`, поэтому проверяйте результаты через
`npm run serve`: в dev-режиме `npm start` полноценный индекс не создаётся.
Поиск не требует ключей или внешних сервисов; на Vercel его индекс защищён тем же
middleware, что и остальные файлы сайта.

В `package.json` зафиксирован Webpack 5.105.4: новые версии несовместимы с
`webpackbar` 6 в Docusaurus 3.9.2 и приводят к `ProgressPlugin ValidationError`.
Не удаляйте override без совместного обновления Docusaurus и проверки запуска.

## Локальный запуск

1. Установите Node.js 22 LTS или 24 LTS вместе с npm.
2. Windows: запустите `docs\start.bat` (двойным щелчком или из терминала).
3. Linux/macOS/Git Bash: выполните `sh docs/start.sh`.
4. Откройте http://localhost:3000. Проверьте текст и диаграмму, в том числе в тёмной теме.
5. Изменения страниц автоматически отображаются в браузере. Для остановки нажмите Ctrl+C.

Скрипты сами переходят в свою папку и устанавливают зависимости через `npm ci`, если
`node_modules` отсутствует. После изменения `package-lock.json` выполните `npm ci` в `docs`
вручную. Другой порт: `docs\start.bat --port 3001` или `sh docs/start.sh --port 3001`.

Локальный Docusaurus показывает документацию без пароля. Vercel middleware в этом режиме
не выполняется; это режим проверки рендера, а не проверки защиты.

Проверка production-сборки (из `docs`):

```sh
npm ci
npm test
npm run build
npm run serve
```

## Первый деплой на бесплатный Vercel

1. Создайте **приватный** Git-репозиторий на GitHub/GitLab/Bitbucket и загрузите туда проект.
   Приватность репозитория важна: middleware защищает сайт, но не исходные Markdown в Git.
   Не добавляйте секреты в файлы и коммиты.
2. В Vercel выберите **Add New → Project**, подключите Git-провайдера и импортируйте репозиторий.
3. Укажите **Root Directory = `docs`**. Доступ к исходникам вне Root Directory не требуется.
4. Framework Preset: **Docusaurus (v2)** (этот идентификатор используется и для Docusaurus 3).
   Настройки из `docs/vercel.json`: Install Command `npm ci`, Build Command `npm run build`,
   Output Directory `build`. Node.js Version: **22.x** или **24.x**.
5. До первого деплоя добавьте Environment Variables:

   | Имя | Значение | Окружения |
   | --- | --- | --- |
   | `DOCS_AUTH_USER` | Например, `reader`; без двоеточия | Production и Preview |
   | `DOCS_AUTH_PASSWORD` | Длинный случайный пароль из менеджера паролей | Production и Preview |
   | `DOCS_SITE_URL` | `https://имя-проекта.vercel.app` или собственный домен, без завершающего `/` | Production и Preview |

   Используйте обычные серверные переменные: не добавляйте префикс `NEXT_PUBLIC_` или
   `DOCUSAURUS_` и не помещайте пароль в `customFields` конфигурации Docusaurus.
   `.env.example` — только перечень переменных; локальные скрипты его не загружают.
   Если домен ещё неизвестен, после первого деплоя задайте `DOCS_SITE_URL` и выполните Redeploy.
6. Нажмите **Deploy**. В **Settings → Git** проверьте Production Branch (обычно `main`).
7. В **Settings → Build and Deployment → Ignored Build Step** проверьте команду
   `node scripts/ignore-build.mjs`. Она также задана в `vercel.json` как `ignoreCommand`.
   Не заменяйте её сравнением только с предыдущим коммитом: в push может быть несколько коммитов.
   Проверьте, что автоматическое предоставление System Environment Variables включено:
   скрипт использует `VERCEL_GIT_PREVIOUS_SHA`. Без него сборки будут запускаться всегда.
8. Не включайте платную Password Protection. Наш middleware доступен на Hobby и
   выполняется перед выдачей статических ресурсов. Использование подчиняется квотам Hobby.
   Если включена Vercel Authentication для Preview, перед нашим запросом пароля может появиться
   вход в аккаунт Vercel; это отдельная настройка Deployment Protection.

## Проверка защиты после деплоя

Откройте сайт в приватном окне. Браузер должен запросить логин и пароль. Неверный пароль
не должен открывать страницу. После успешного входа должны отображаться текст и Mermaid.
В DevTools → Network скопируйте URL одного JS-файла и проверьте его без учётных данных:

```sh
curl -I https://имя-проекта.vercel.app/
curl -I https://имя-проекта.vercel.app/assets/js/ИМЯ-ФАЙЛА.js
curl -I -u reader https://имя-проекта.vercel.app/
```

Первые два запроса должны вернуть **401**, последний запрос спросит пароль в терминале и
при верных данных вернёт **200**. Проверьте также Preview URL и собственный домен, если он есть.
Без любой из двух переменных авторизации ожидается **503**, а не публичный сайт.
Не публикуйте реальные документы, пока эта проверка не пройдёт.

Basic Auth использует стандартное окно браузера, без отдельной страницы входа. Используйте
HTTPS. Браузер может помнить учётные данные; для завершения сеанса закройте приватное окно.
Смена пароля: измените переменную в Vercel и выполните **Redeploy**. Изменение переменной
не обновляет старые деплои: удалите старые доступные deployments, если требуется отозвать
доступ по прежнему паролю. Общий пароль не предоставляет отдельных аккаунтов и MFA.

## Автодеплой

После подключения Git push в production-ветку автоматически запускает production deployment,
а push в другие ветки / pull request — Preview (согласно настройкам Git-интеграции Vercel).
`ignore-build.mjs` сравнивает `docs` с `VERCEL_GIT_PREVIOUS_SHA` последнего успешного деплоя:

- изменения в `docs` запускают сборку;
- изменения только вне `docs` пропускают сборку;
- при первом деплое или недоступной истории сборка запускается, чтобы не потерять обновление.

Чтобы проверить, измените тестовый текст, выполните commit/push и дождитесь Ready в Vercel.
Затем измените файл вне `docs`, выполните commit/push и проверьте пропуск сборки. Если
предыдущий SHA недоступен (например, из-за shallow clone), возможна лишняя безопасная сборка.
При ручном Redeploy при необходимости отключите Ignored Build Step в настройках проекта.

## Структура

```text
docs/
  AGENTS.md                # Правила структуры для агентов
  documents/drafts/index.md # Тестовая страница с Mermaid
  documents/drafts/_category_.yml # Название и порядок раздела
  documents/contracts/_category_.yml # Пока пустой раздел
  documents/contracts/index.md # Документ-заглушка
  docusaurus.config.js      # Docusaurus и Mermaid
  sidebars.js              # Автоматическая навигация
  scripts/sections.cjs      # Разделы шапки и боковые меню из папок/YAML
  middleware.js            # Защита всех URL в Vercel
  scripts/auth.cjs         # Серверная проверка Basic Auth (CommonJS для Vercel)
  scripts/ignore-build.mjs # Пропуск сборок без изменений docs
  tests/auth.test.mjs      # Проверки авторизации
  start.bat / start.sh     # Локальный запуск
  vercel.json              # Конфигурация деплоя
```

## Ошибка загрузки middleware

Если runtime-логи показывают `ERR_REQUIRE_ESM` при загрузке middleware, убедитесь,
что задеплоен актуальный `middleware.js` с импортом `scripts/auth.cjs`, а не старого
`auth.mjs`. Vercel преобразует entrypoint в CommonJS, поэтому модуль авторизации
сохранён в совместимом формате `.cjs`. Не добавляйте `"type": "module"` в общий
`package.json`: это ранее ломало клиентскую сборку Docusaurus. После исправления
нужен commit/push и новый deployment; изменения переменных окружения не требуются.

## Официальные источники

- [Vercel Routing Middleware: доступен на всех тарифах](https://vercel.com/docs/routing-middleware)
- [Vercel Ignored Build Step](https://vercel.com/docs/project-configuration/project-settings)
- [Docusaurus Mermaid](https://docusaurus.io/docs/markdown-features/diagrams)
