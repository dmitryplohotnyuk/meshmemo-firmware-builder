import json
from pathlib import Path
import socket

import pytest

from meshmemo_builder import cli, wizard
from meshmemo_builder.pipeline import BuilderError, resolve_plan
from meshmemo_builder.recovery import prepare_recovery, verify_recovery
from test_update import build_files, flash_file, mutate, ota


def session(answers, execute=None):
    answers = iter(answers)
    output, calls = [], []
    def reader(prompt):
        output.append(prompt)
        try: return next(answers)
        except StopIteration: raise EOFError
    def runner(args):
        calls.append(args)
        return execute(args) if execute else 0
    code = wizard.run(reader=reader, writer=output.append, execute=runner)
    return code, "\n".join(output), calls


def test_defaults_keep_options_off_and_preview_before_execution(tmp_path):
    workspace = tmp_path / "source tree with spaces"
    code, output, calls = session(["1", "1", "", "", "", "", str(workspace), "", "да"])
    assert code == 0 and len(calls) == 1
    args = calls[0]
    assert args[:3] == ["prepare", "--board", "tbeam-s3-core"]
    assert str(workspace) in args
    assert not any(flag in args for flag in ("--ua22", "--cyrillic", "--display-timeout"))
    assert "Дополнительные опции: выключены" in output
    assert output.index("Начать") < output.index("Подготовка исходников")
    assert not workspace.exists()  # Fake executor used; prompts do not create anything.


@pytest.mark.parametrize("answers, flags", [(["да", "нет", "нет"], ["--ua22"]),
    (["нет", "да", "нет"], ["--cyrillic"]), (["нет", "нет", "да"], ["--display-timeout"]),
    (["да", "да", "да"], ["--ua22", "--cyrillic", "--display-timeout"])])
def test_heltec_independent_options_are_passed_exactly(tmp_path, answers, flags):
    code, _, calls = session(["1", "2", *answers, "1", str(tmp_path / "work"), "1", "да"])
    assert code == 0
    assert calls[0][2] == "heltec-v3"
    assert [arg for arg in calls[0] if arg in ("--ua22", "--cyrillic", "--display-timeout")] == flags


def test_confirmation_default_does_not_start_preparation(tmp_path):
    code, _, calls = session(["1", "1", "", "", "", "", str(tmp_path / "work"), "", ""])
    assert code == 0 and not calls


def test_screenless_board_does_not_ask_for_display_options(tmp_path):
    code, output, calls = session(["1", "3", "да", "1", str(tmp_path / "work"), "1", "да"])
    assert code == 0 and calls[0][2] == "heltec-wsl-v3"
    assert "--ua22" in calls[0]
    assert "--cyrillic" not in calls[0] and "--display-timeout" not in calls[0]
    assert "Включить экранную кириллицу" not in output and "Включить гашение" not in output


@pytest.mark.parametrize("answers", [["q"], ["1", "q"], [], ["1", "1", ""]])
def test_exit_and_eof_stop_without_side_effects(answers):
    assert session(answers)[0::2] == (130, [])


def test_invalid_answers_are_reprompted():
    code, output, calls = session(["", "-1", "99999999999999999999999999999999999999", "foo", "6"])
    assert code == 0 and not calls
    assert output.count("Введите номер") == 4


def test_existing_destination_and_bad_yes_answer_are_reprompted(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    fresh = tmp_path / "new"
    code, output, calls = session(["1", "1", "maybe", "", "", "", "1", str(existing), str(fresh), "1", "да"])
    assert code == 0 and str(fresh) in calls[0]
    assert "Ответьте да" in output and "уже существует" in output
    assert not list(existing.iterdir())


def full_answers(tmp_path):
    return ["1", "1", "", "", "", "2", str(tmp_path / "work"), "1",
            str(tmp_path / "cache"), str(tmp_path / "runtime"), str(tmp_path / "result"), "4", "да"]


def test_full_build_runs_ordered_cli_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("meshmemo_builder.environment.check_host", lambda lock: None)
    code, output, calls = session(full_answers(tmp_path))
    assert code == 0
    assert [args[0] for args in calls] == ["prepare", "fetch-dependencies", "bootstrap", "build"]
    assert "Недостающие зависимости" in output


@pytest.mark.parametrize("fail_at", range(4))
def test_failed_step_never_starts_later_steps(tmp_path, monkeypatch, fail_at):
    monkeypatch.setattr("meshmemo_builder.environment.check_host", lambda lock: None)
    count = []
    def execute(args):
        count.append(args)
        return 1 if len(count) == fail_at + 1 else 0
    code, output, calls = session(full_answers(tmp_path), execute)
    assert code == 1 and len(calls) == fail_at + 1
    assert "Следующие этапы не запускались" in output


def test_unsupported_build_host_fails_before_any_download(tmp_path, monkeypatch):
    def unsupported(lock): raise BuilderError("Unsupported test host")
    monkeypatch.setattr("meshmemo_builder.environment.check_host", unsupported)
    code, output, calls = session(full_answers(tmp_path))
    assert code == 1 and not calls and "Unsupported test host" in output
    assert not list(tmp_path.iterdir())


def test_local_paths_passed_as_arguments_without_shell_interpolation(tmp_path):
    firmware, protobufs = tmp_path / "firmware & data", tmp_path / "proto ; data"
    firmware.mkdir(); protobufs.mkdir()
    code, _, calls = session(["1", "1", "", "", "", "1", str(tmp_path / "work"), "2",
                             f'"{firmware}"', f'"{protobufs}"', "да"])
    assert code == 0
    assert calls[0][-4:] == ["--firmware-source", str(firmware), "--protobuf-source", str(protobufs)]


def test_nested_output_paths_refused_before_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("meshmemo_builder.environment.check_host", lambda lock: None)
    answers = full_answers(tmp_path)
    answers[10] = str(tmp_path / "work" / "result")
    code, output, calls = session(answers)
    assert code == 1 and not calls and "не вложенными" in output


def test_update_flow_saves_real_plan_and_reprompts_existing_output(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Network access"))
    backup = flash_file(tmp_path)
    manifest, _ = build_files(tmp_path)
    output = tmp_path / "plan.json"
    old = backup.read_bytes()
    code, text, calls = session(["2", "1", str(backup), str(manifest), "да", str(backup), str(output)])
    assert code == 0 and not calls and "Запас:" in text
    assert backup.read_bytes() == old
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "offline-compatible"


def test_blocked_update_is_not_presented_as_ready(tmp_path):
    backup = flash_file(tmp_path, slots=(ota(2, 1), ota(0)))
    manifest, _ = build_files(tmp_path)
    code, output, calls = session(["2", "1", str(backup), str(manifest), "нет"])
    assert code == 2 and not calls
    assert "заблокирован" in output and "Проверка файлов пройдена" not in output


def test_wrong_board_manifest_produces_error(tmp_path):
    backup = flash_file(tmp_path)
    manifest, _ = build_files(tmp_path, board="heltec-v3")
    code, output, calls = session(["2", "1", str(backup), str(manifest)])
    assert code == 1 and not calls and "board" in output


def test_recovery_create_and_verify_from_wizard(tmp_path):
    backup = flash_file(tmp_path)
    destination = tmp_path / "package"
    code, output, calls = session(["3", str(backup), str(destination), "да"])
    assert code == 0 and not calls and "настройки и ключи" in output
    assert verify_recovery(destination)["status"] == "verified"
    code, output, _ = session(["4", str(destination)])
    assert code == 0 and "Пакет проверен: 5 файлов" in output


def test_recovery_cancel_preserves_source(tmp_path):
    backup = flash_file(tmp_path)
    destination = tmp_path / "package"
    code, _, _ = session(["3", str(backup), str(destination), ""])
    assert code == 0 and not destination.exists()


def test_bad_backup_blocks_recovery_before_destination_prompt(tmp_path):
    backup = flash_file(tmp_path)
    mutate(backup, 0x10000, b"\0")
    code, output, _ = session(["3", str(backup)])
    assert code == 2 and "Пакет не создан" in output


def test_invalid_package_does_not_report_success(tmp_path):
    code, output, _ = session(["4", str(tmp_path)])
    assert code == 1 and "Пакет проверен" not in output


def test_build_existing_uses_prepared_options(tmp_path, monkeypatch):
    work, runtime = tmp_path / "work", tmp_path / "runtime"
    work.mkdir(); runtime.mkdir()
    plan = resolve_plan(options=["cyrillic"])
    monkeypatch.setattr("meshmemo_builder.build.read_prepared", lambda path: {"plan": plan})
    code, output, calls = session(["5", str(work), str(runtime), str(tmp_path / "output"), "0", "4", "да"])
    assert code == 0 and len(calls) == 1 and calls[0][0] == "build"
    assert "cyrillic" in output and "от 1 до 64" in output


def test_interruption_during_execution_returns_130_and_stops_chain(tmp_path, monkeypatch):
    monkeypatch.setattr("meshmemo_builder.environment.check_host", lambda lock: None)
    def execute(args): raise KeyboardInterrupt
    code, output, calls = session(full_answers(tmp_path), execute)
    assert code == 130 and len(calls) == 1 and "Мастер остановлен" in output


def test_cli_requires_terminal_and_dispatches_to_wizard(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["wizard"]) == 1
    assert "interactive terminal" in capsys.readouterr().err
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard, "run", lambda: 130)
    assert cli.main(["wizard"]) == 130
