"""Small terminal wizard over the existing CLI and offline validators."""

import json
from pathlib import Path

from .pipeline import BuilderError, load_catalog, resolve_plan


class Cancelled(Exception):
    pass


class Prompts:
    def __init__(self, reader, writer):
        self.reader, self.write = reader, writer

    def ask(self, label, default=""):
        value = self.reader(f"{label}" + (f" [{default}]" if default else "") + ": ").strip()
        if value.lower() in ("q", "выход"):
            raise Cancelled
        return value or default

    def choose(self, label, choices, default=""):
        self.write(label)
        for index, text in enumerate(choices, 1):
            self.write(f"  {index}. {text}")
        while True:
            value = self.ask("Номер", default)
            if value in {str(index) for index in range(1, len(choices) + 1)}:
                return int(value) - 1
            self.write("Введите номер из списка; q — выход.")

    def yes(self, label):
        while True:
            value = self.ask(label + " (да/нет)", "нет").lower()
            if value in ("да", "д", "yes", "y"):
                return True
            if value in ("нет", "н", "no", "n"):
                return False
            self.write("Ответьте да или нет; Enter означает нет.")

    def path(self, label, kind, default=""):
        while True:
            value = self.ask(label, default)
            if len(value) > 1 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if not value:
                self.write("Укажите путь.")
                continue
            path = Path(value).expanduser()
            if (kind == "file" and path.is_file()) or (kind in ("directory", "cache") and path.is_dir()):
                return path
            if kind in ("new", "new-file", "new-package", "cache"):
                if path.exists() or path.is_symlink():
                    self.write("Этот путь уже существует. Выберите новый, чтобы сохранить его содержимое.")
                elif kind in ("new-file", "new-package") and not path.parent.is_dir():
                    self.write("Родительская папка должна существовать.")
                else:
                    return path
            else:
                self.write("Файл или папка не найдены.")

    def jobs(self):
        while True:
            value = self.ask("Количество задач компиляции", "4")
            if value.isascii() and value.isdecimal() and len(value) <= 2 and 1 <= int(value) <= 64:
                return value
            self.write("Введите число от 1 до 64.")


def board_choice(ui, profile="meshmemo", architecture=None):
    boards = load_catalog()["boards"]
    names = [name for name, board in boards.items()
             if profile in board.get("supported_profiles", load_catalog()["profiles"])
             and (architecture is None or board["architecture"] == architecture)]
    return names[ui.choose("Выберите плату", [boards[name]["name"] for name in names])]


def distinct_paths(paths):
    resolved = [path.resolve() for path in paths]
    for index, path in enumerate(resolved):
        if any(path == other or path.is_relative_to(other) or other.is_relative_to(path)
               for other in resolved[:index]):
            raise BuilderError("Рабочая папка, runtime, кеш и результат должны быть отдельными, не вложенными каталогами")


def execute_steps(ui, steps, execute):
    for title, args in steps:
        ui.write(f"\n{title}")
        code = execute(args)
        if code:
            ui.write("Этап завершился с ошибкой. Следующие этапы не запускались; созданные файлы сохранены для проверки.")
            return code
    ui.write("Готово.")
    return 0


def prepare_flow(ui, execute, profile="meshmemo"):
    board = board_choice(ui, profile)
    catalog = load_catalog()
    hardware = catalog["boards"][board]
    upstreams = hardware["upstreams"]
    available = hardware.get("supported_options", catalog["options"])
    if hardware.get("display") == "none":
        ui.write("У этого профиля нет штатного экрана; экранные опции недоступны.")
    upstream = upstreams[0] if len(upstreams) == 1 else upstreams[ui.choose("Версия Meshtastic", upstreams)]
    options = []
    for name, label in (("ua22", "Включить UA22 — предел мощности UA_433 22 dBm"),
                        ("cyrillic", "Включить экранную кириллицу"),
                        ("display-timeout", "Включить гашение экрана по таймеру при USB-подключении")):
        if name in available and ui.yes(label):
            options.append(name)
    plan = resolve_plan(board, upstream, profile, options)
    full = ui.choose("Что подготовить", ["Исходники с патчами", "Полную сборку прошивки"], "1") == 1
    if full:
        from .environment import check_host, load_lock
        check_host(load_lock(plan))  # Fail before downloads or directory creation.
    workspace = ui.path("Новая рабочая папка", "new", f".work/{board}-wizard")
    local = ui.choose("Источник исходников", ["Скачать закреплённые версии из Git", "Локальные Git-репозитории"], "1") == 1
    sources = []
    source_paths = []
    if local:
        for flag, label in (("--firmware-source", "Git-папка firmware"), ("--protobuf-source", "Git-папка protobufs")):
            path = ui.path(label, "directory")
            source_paths.append(path)
            sources.extend([flag, str(path)])
    command = ["prepare", "--board", board, "--upstream", upstream, "--profile", profile,
               "--destination", str(workspace)] + [f"--{name}" for name in options] + sources
    steps = [("Подготовка исходников", command)]
    outputs = [workspace]
    if full:
        cache = ui.path("Папка кеша зависимостей", "cache", ".cache/dependencies")
        runtime = ui.path("Новая папка окружения сборки", "new", f".work/rt-{hardware['hardware_model']}")
        output = ui.path("Новая папка готовой прошивки", "new", f"dist/{board}-wizard")
        jobs = ui.jobs()
        outputs.extend([cache, runtime, output])
        steps.extend([
            ("Загрузка и проверка зависимостей", ["fetch-dependencies", "--workspace", str(workspace), "--cache", str(cache)]),
            ("Подготовка окружения", ["bootstrap", "--workspace", str(workspace), "--cache", str(cache), "--destination", str(runtime)]),
            ("Компиляция", ["build", "--workspace", str(workspace), "--runtime", str(runtime), "--output", str(output), "--jobs", jobs]),
        ])
    distinct_paths(outputs)
    for source in source_paths:
        distinct_paths([source, *outputs])
    label = "MeshMemo: USF2 1/4" if profile == "meshmemo" else "Только выбранные патчи, без MeshMemo"
    ui.write(f"\nПлата: {plan['hardware']['name']}. Meshtastic: {upstream}. {label}.")
    ui.write("Дополнительные опции: " + (", ".join(options) if options else "выключены"))
    ui.write(f"Исходники: {workspace}")
    if not local:
        ui.write("Исходники будут скачаны из закреплённых Git-репозиториев.")
    if full:
        ui.write(f"Окружение: {runtime}. Результат: {output}. Кеш: {cache}.")
        ui.write("Недостающие зависимости будут скачаны; компиляция может занять несколько минут.")
    if not ui.yes("Начать"):
        ui.write("Отменено. Подготовка не запускалась.")
        return 0
    return execute_steps(ui, steps, execute)


def build_flow(ui, execute):
    from .build import read_prepared
    workspace = ui.path("Подготовленная рабочая папка", "directory")
    prepared = read_prepared(workspace.resolve())
    runtime = ui.path("Готовое окружение сборки", "directory")
    output = ui.path("Новая папка результата", "new", "dist/wizard-build")
    distinct_paths([workspace, runtime, output])
    jobs = ui.jobs()
    ui.write(f"\nПлата: {prepared['plan']['board']}. Опции: {', '.join(prepared['plan']['options']) or 'выключены'}.")
    ui.write(f"Профиль: {prepared['plan']['profile']}.")
    ui.write(f"Результат: {output}")
    if not ui.yes("Начать компиляцию"):
        ui.write("Отменено. Компиляция не запускалась.")
        return 0
    return execute_steps(ui, [("Компиляция", ["build", "--workspace", str(workspace), "--runtime", str(runtime),
                                              "--output", str(output), "--jobs", jobs])], execute)


def release_flow(ui, execute, profile="meshmemo"):
    board = board_choice(ui, profile)
    channel = ("stable", "preview")[ui.choose("Канал Meshtastic", ["Стабильный", "Предварительный (preview)"], "1")]
    tag = ui.ask("Точный тег релиза или latest для самого свежего в выбранном канале", "latest")
    catalog = load_catalog()
    available = catalog["boards"][board].get("supported_options", catalog["options"])
    options = []
    for name, label in (("ua22", "Включить UA22"), ("cyrillic", "Включить экранную кириллицу"),
                        ("display-timeout", "Включить таймер экрана при USB-подключении")):
        if name in available and ui.yes(label):
            options.append(name)
    workspace = ui.path("Новая папка исходников релиза", "new", ".work/new-release")
    ui.write(f"\nПлата: {board}. Канал: {channel}. Релиз: {tag}.")
    if profile == "patches-only":
        ui.write("Только выбранные патчи, без MeshMemo.")
    ui.write("Дополнительные опции: " + (", ".join(options) or "выключены"))
    ui.write("Релиз будет получен из официального репозитория и закреплён по коммиту.")
    ui.write("Результаты применения патчей и проверки зависимостей сохраняются в release.json.")
    ui.write("Новая версия считается экспериментальной. Конфликт требует адаптации патча;")
    ui.write("изменение зависимостей требует нового проверенного окружения для сборки.")
    if not ui.yes("Скачать и применить патчи"):
        ui.write("Отменено. Загрузка не запускалась.")
        return 0
    return execute_steps(ui, [("Получение и патчинг релиза", ["prepare-release", "--board", board,
        "--release", tag, "--channel", channel, "--destination", str(workspace), "--profile", profile]
        + [f"--{name}" for name in options])], execute)


def update_flow(ui):
    from .update import plan_update
    board = board_choice(ui, architecture="esp32-s3")
    backup = ui.path("Полная flash-копия (.bin)", "file")
    manifest = ui.path("Manifest готовой сборки (.json)", "file")
    report = plan_update(backup, manifest, board)
    ui.write("\nОпции выбранной сборки: " + (", ".join(report["options"]) or "выключены"))
    if report["blockers"]:
        ui.write("План обновления заблокирован:")
        for reason in report["blockers"]:
            ui.write("  - " + reason)
    else:
        write = report["proposed_write"]
        ui.write(f"Проверка файлов пройдена. Адрес: 0x{write['offset']:x}. Образ: {write['size_bytes']} байт.")
        ui.write(f"Раздел: {write['partition_size_bytes']} байт. Запас: {write['remaining_bytes']} байт.")
    ui.write("Это проверка сохранённых файлов. Перед записью нужны проверки подключённой платы из отчёта.")
    if ui.yes("Сохранить JSON-отчёт"):
        path = ui.path("Новый файл отчёта", "new-file", "update-plan.json")
        with path.open("x", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        ui.write(f"Отчёт: {path}")
    return 2 if report["blockers"] else 0


def recovery_flow(ui):
    from .recovery import prepare_recovery
    from .update import inspect_flash
    backup = ui.path("Полная flash-копия (.bin)", "file")
    inspection = inspect_flash(backup)
    if inspection["blockers"]:
        ui.write("Пакет не создан:")
        for reason in inspection["blockers"]:
            ui.write("  - " + reason)
        return 2
    ui.write(f"Копия: {inspection['flash_size_bytes']} байт. Приложение: {inspection['installed_image']['size_bytes']} байт.")
    destination = ui.path("Новая папка пакета (родительская папка должна существовать)", "new-package", "recovery-package")
    ui.write(f"Пакет: {destination}. Он будет содержать настройки и ключи из копии; выберите приватную папку.")
    if not ui.yes("Создать пакет восстановления"):
        ui.write("Отменено. Пакет не создавался.")
        return 0
    result = prepare_recovery(backup, destination)
    ui.write(f"Пакет создан и проверен: {result}")
    return 0


def verify_flow(ui):
    from .recovery import verify_recovery
    package = ui.path("Папка пакета восстановления", "directory")
    report = verify_recovery(package)
    ui.write(f"Пакет проверен: {report['files_verified']} файлов. Адрес приложения: 0x{report['application']['offset']:x}.")
    ui.write("Принадлежность копии плате и возможность записи проверяются отдельно перед восстановлением.")
    return 0


def run(*, reader=None, writer=None, execute=None):
    if execute is None:
        from .cli import main
        execute = main
    ui = Prompts(reader or input, writer or print)
    ui.write("MeshMemo — пошаговый мастер. q — выход на любом шаге.")
    ui.write("Мастер подготавливает файлы и сборки; прошивка устройства не выполняется.")
    try:
        task = ui.choose("Выберите действие", ["Подготовить новую прошивку", "Проверить обновление по flash-копии",
            "Создать пакет восстановления", "Проверить пакет восстановления", "Собрать подготовленные исходники",
            "Получить свежий релиз и применить патчи", "Только дополнительные патчи (без MeshMemo)", "Выйти"])
        if task == 0: return prepare_flow(ui, execute)
        if task == 1: return update_flow(ui)
        if task == 2: return recovery_flow(ui)
        if task == 3: return verify_flow(ui)
        if task == 4: return build_flow(ui, execute)
        if task == 5: return release_flow(ui, execute)
        if task == 6:
            source = ui.choose("Версия прошивки", ["Закреплённая версия", "Свежий опубликованный релиз"], "1")
            return (prepare_flow if source == 0 else release_flow)(ui, execute, profile="patches-only")
        return 0
    except (Cancelled, EOFError, KeyboardInterrupt):
        ui.write("\nМастер остановлен. Завершённые результаты и журналы сохранены; новые этапы не запускаются.")
        return 130
    except (BuilderError, OSError) as exc:
        ui.write(f"Ошибка: {exc}")
        return 1
