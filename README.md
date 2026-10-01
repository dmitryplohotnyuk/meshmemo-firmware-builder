# MeshMemo Firmware Builder

CLI для подготовки и сборки Meshtastic с расширением MeshMemo. Профили:
**T-Beam S3 Core и Heltec V3, Meshtastic 2.7.26**, протокол USF2, профиль 1/4.
Требуется [MeshMemo server](https://github.com/dmitryplohotnyuk/meshmemo)
**0.1.0a8+**; сервер обновляется первым. Другие платы
и версии пока отклоняются. Сборщик работает с закреплёнными исходниками.

## Независимые дополнительные опции

| Флаг | Изменение | По умолчанию |
| --- | --- | --- |
| `--ua22` | Программный предел мощности UA_433 — 22 dBm | Выключено |
| `--cyrillic` | OLED_UA и исправление отображения `Ґ/ґ` | Выключено |
| `--display-timeout` | Гашение дисплея по таймеру при сохранении USB-сессии | Выключено |

Опции можно сочетать произвольно. Без `--display-timeout` сохраняется штатное
поведение дисплея; этот флаг не отключает сам дисплей. Кириллица относится к OLED,
а UTF-8-сообщения доступны и без неё. UA22 не меняет выбранный на устройстве регион.

Базовый профиль включает USB handshake, replay, исправление USB polling и
server-channel replies. Windows LTO workaround выбирается отдельно через
`--windows-workaround auto|on|off`; `auto` включает его на Windows. Он не включает
ни одну из трёх пользовательских опций.

## Установка и просмотр

Требуются Python 3.11+ и Git. Выполнять в активированном Python-окружении:

```sh
git clone https://github.com/dmitryplohotnyuk/meshmemo-firmware-builder.git
cd meshmemo-firmware-builder
python -m pip install .
meshmemo-builder doctor
meshmemo-builder list-boards
meshmemo-builder list-options
meshmemo-builder inspect --board tbeam-s3-core
meshmemo-builder inspect --board heltec-v3
```

Для пошаговой работы в терминале:

```sh
meshmemo-builder wizard
```

Мастер на русском языке помогает выбрать плату и опции, подготовить исходники
или полную сборку, проверить обновление по flash-копии и создать/проверить пакет
восстановления. UA22, кириллица и таймер дисплея остаются выключенными до явного
выбора. Перед подготовкой/сборкой показываются параметры и пути результата.
Enter в вопросе «да/нет» означает «нет», `q` отменяет работу на любом шаге.

Для полной новой сборки нужны Windows AMD64 и Python 3.12.14; этот сценарий
проверяет окружение до скачивания исходников. Подготовка исходников и работа
с сохранёнными копиями доступны на Python 3.11+. При выборе загрузки мастер
скачивает закреплённые исходники и недостающие зависимости; компиляция работает
через существующий проверенный CLI. После ошибки следующие этапы не запускаются.

Мастер выполняет один выбранный сценарий за запуск. Готовую рабочую папку можно
позже собрать через пункт «Собрать подготовленные исходники». Обычные команды
CLI остаются доступны для скриптов; мастер требует интерактивный терминал.
Прошивка устройства и подключение к MeshMemo-серверу в нём не выполняются.

Wheel содержит реестр, патчи и дополнительные исходники. Для работы установленного
пакета соседняя папка MeshMemo не нужна. `doctor` проверяет Python/Git для
подготовки; сборка отдельно проверяет версию PlatformIO.

## Подготовка прошивки

Базовый профиль:

```sh
meshmemo-builder prepare --board tbeam-s3-core --destination .work/base
```

С выбранными дополнительными опциями:

```sh
meshmemo-builder prepare --board tbeam-s3-core --ua22 --cyrillic --display-timeout --destination .work/custom
```

Для локального Git cache есть `--firmware-source` и `--protobuf-source`.
Их содержимое всё равно извлекается из закреплённых commit. Незакоммиченные
изменения исходного репозитория не копируются. Использование уже существующего
destination отклоняется; после ошибки выбирайте новую папку.

Результат: `firmware/` и `prepare.json` со статусом, выбранными опциями, commit,
SHA-256 патчей и подготовленных исходников. Только `status=prepared` означает
завершённую подготовку. Сначала проверяются хеши, затем применимость каждого патча.

## Сборка

Текущий lock рассчитан на **Windows AMD64 и Python 3.12.14**. Зависимости
скачиваются отдельно, затем устанавливаются и используются без сети:

```sh
meshmemo-builder fetch-dependencies --workspace .work/base --cache .cache/dependencies
meshmemo-builder bootstrap --workspace .work/base --cache .cache/dependencies --destination .work/runtime
meshmemo-builder build --workspace .work/base --runtime .work/runtime --output dist/base --jobs 4
```

Lock содержит точные версии и SHA-256 платформы, toolchain, библиотек и Python
пакетов, включая транзитивные. `bootstrap` создаёт собственные venv и PlatformIO
каталоги; глобальные кеши не используются. До и после компиляции проверяются
хеши установленного окружения. Подмена или отсутствие зависимости останавливает
работу. Неудачную установку нужно повторять в новом каталоге.
На Windows временные короткие пути создаются на свободных буквах дисков и
освобождаются после сборки. Для Windows checkout нужно подготовить с включённым
Windows workaround. Изменённые после подготовки исходники отклоняются.

Сборщик выдаёт application-only `.bin`, `manifest.json`, `prepare.json`,
`dependencies.txt`, `dependency-lock.json`, `runtime.json` и `build.log`.
Проверяется размер приложения относительно
**сгенерированной таблицы разделов**, а не исторической константы конкретной ноды.
Это не подтверждает соответствие разметке подключённого устройства.

Команды не открывают serial port и не прошивают устройство.

## Проверка обновления без платы

```sh
meshmemo-builder inspect-flash --backup backups/flash-before.bin
meshmemo-builder plan-update --board tbeam-s3-core --backup backups/flash-before.bin --manifest dist/custom/manifest.json --output update-plan.json
```

Проверяются полная flash-копия, разделы/OTA, целостность старого и нового
приложения и соответствие manifest выбранной плате. План показывает адрес,
диапазон стирания и запас места по фактической разметке копии. Неоднозначный
слот, повреждение или переполнение блокируют план. Исходные файлы сохраняются.
`offline-compatible` относится только к файлам и не разрешает запись устройства.
Подробнее: [формат отчёта, ограничения и коды CLI](docs/offline-update.md).

Из проверенной копии можно подготовить самодостаточный пакет восстановления:

```sh
meshmemo-builder prepare-recovery --backup backups/flash-before.bin --destination backups/tbeam-recovery
meshmemo-builder verify-recovery --package backups/tbeam-recovery
```

В новой папке сохраняются полная flash-копия, прежнее приложение, manifest,
SHA-256 и инструкция. Пакет содержит настройки и ключи; храните его приватно.
Незавершённые или изменённые пакеты отклоняются. Проверка не запускает прошивку.

Запись прошивки на устройство остаётся следующим этапом; терминальный мастер
уже доступен командой `meshmemo-builder wizard`.

## Структура

```text
src/meshmemo_builder/       CLI, подготовка и компиляция
registry/catalog.json      закреплённые версии, плата, профиль и опции
registry/locks/            версии, URL и SHA-256 всех зависимостей сборки
patches/core/              общий MeshMemo USB bridge
patches/platform/          USB polling и Windows workaround
patches/optional/          UA22, OLED Cyrillic, display timeout
payloads/replay/           radio adapter и encoder
payloads/portable/         transport policy и ESP32 SHA-256 backend
tests/                    модульные, upstream и native проверки
docs/provenance.json        происхождение и хеши импортированных файлов
docs/implementation-plan.md план и текущий охват
.work/                    изолированные checkout и логи (не в Git)
.cache/                   toolchain cache (не в Git)
dist/                     артефакты (не в Git)
```

## Проверки и ограничения

```sh
python -m pip install ".[dev]"
python -m pytest -q
python -m build
```

Upstream integration tests требуют переменные `MESHMEMO_TEST_FIRMWARE` и
`MESHMEMO_TEST_PROTOBUFS` с путями к локальным Git-репозиториям, содержащим нужные
commit. Сравнение с прежней полной сборкой использует `MESHMEMO_TEST_LEGACY`;
native проверки — `MESHMEMO_TEST_HARNESS`. Без этих входов соответствующие тесты
явно пропускаются. Подробности результатов: [validation.md](docs/validation.md).

В Visual Studio Developer PowerShell команда `./scripts/test-native.ps1`
компилирует harness обеих плат из `.work/portable-tbeam` и `.work/portable-heltec`
(заранее подготовленных командой `prepare`), а также семь transport policy
проверок. Пути можно передать параметрами `-TBeamWorkspace`, `-HeltecWorkspace`,
`-OutputDirectory`. Затем задайте `MESHMEMO_TEST_HARNESS` на абсолютный путь к
`.work/native-portable/tbeam/bridge.exe` перед pytest.

Firmware сообщает реальную модель и build ID. Сервер проверяет USF2 capabilities;
привязка к модели 12 сохранена только для старых USF1 1/2–1/3.
T-Beam использует native USB CDC, Heltec V3 — console через USB–UART.
SHA-256 имеет отдельный ESP32 backend; другие архитектуры ещё не поддержаны.
Подробности wire contract: [protocol.md](docs/protocol.md).
Lock зависимостей реализован для Windows AMD64; Linux lock пока отсутствует.
Python 3.12.14 и Git устанавливаются отдельно. Lock не фиксирует ОС и не обещает
побайтовую воспроизводимость бинарника. Подробности: [dependencies.md](docs/dependencies.md).

История по радио остаётся экспериментальной. Проверка сборки и native tests не
подтверждает совместимость iPhone, отсутствие дублей, multi-hop или серверные
каналы на оборудовании. Приём и архивирование MeshMemo возможны со штатной
прошивкой; патч нужен для расширенных функций выдачи.

Изменения патчей сборщика ведутся в этом репозитории; прежний `firmware/` сервера
остаётся историческим набором для установленной сборки. Автоматическая синхронизация
между ними не выполняется. Сервер 0.1.0a8 согласован со сборщиком 0.1.0a2.
См. [provenance](docs/provenance.json),
[notices](THIRD_PARTY_NOTICES.md) и [план](docs/implementation-plan.md).

## Лицензия

Код сборщика распространяется под [GPL-3.0-only](LICENSE). Лицензии и уведомления
исходных проектов и зависимостей сохраняются; см. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
