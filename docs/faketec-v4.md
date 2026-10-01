# Faketec V4

`--board faketec-v4` — плата на ProMicro/SuperMini nRF52840, основанная на
[fakeTec](https://github.com/gargomoma/fakeTec_pcb). Ревизия V4 добавляет MOSFET
выходы; [автор V4](https://github.com/gargomoma/fakeTec_pcb/issues/16) указывает
GPIO 24 для GPS enable, 6 и 8 для внешнего оборудования.

Сборщик использует официальный
[nrf52_promicro_diy_tcxo Meshtastic 2.7.26](https://github.com/meshtastic/firmware/tree/54e0d8d0ab2ff56b3a9ce967e53f79e49af560fb/variants/nrf52840/diy/nrf52_promicro_diy_tcxo),
hardware model 63. `TCXO_OPTIONAL` поддерживает оба варианта генератора;
отдельный XTAL target не нужен. Профиль предназначен для стандартной разводки
V4. Настройки GPS, внешних оповещателей и коэффициент измерения батареи
сохраняют upstream-значения и должны соответствовать установленным компонентам.
MiniX, V5/V6, InkHUD и произвольная разводка не проверены этим профилем.

## Подготовка и сборка

Полный MeshMemo, без дополнительных опций:

```sh
meshmemo-builder prepare --board faketec-v4 --destination .work/faketec
```

Только UA22 и/или кириллица, без MeshMemo:

```sh
meshmemo-builder prepare --board faketec-v4 --profile patches-only --ua22 --cyrillic --destination .work/faketec-options
```

Уберите ненужный флаг. Для OLED SSD1306 также доступен `--display-timeout`.
Все опции по умолчанию выключены. UA22 изменяет программный предел UA_433,
но не выбирает регион и не меняет диапазон самого радиомодуля.
В мастер добавлена Faketec V4 для обоих профилей и свежих релизов.

После подготовки выбранного workspace:

```sh
meshmemo-builder fetch-dependencies --workspace .work/faketec --cache .cache/dependencies
meshmemo-builder bootstrap --workspace .work/faketec --cache .cache/dependencies --destination .work/rt-nrf
meshmemo-builder build --workspace .work/faketec --runtime .work/rt-nrf --output dist/faketec
```

Зафиксированное окружение: Windows AMD64, Python 3.12.14, PlatformIO 6.2.0.
Обход LTO для ESP32 не применяется; `--windows-workaround on` отклоняется.
Свежие версии проверяются обычным `prepare-release --board faketec-v4`;
конфликты патчей или изменённые зависимости блокируют готовность к сборке.

## Формат и совместимость

Готовый файл называется `firmware-nrf52_promicro_diy_tcxo-<версия>.uf2`.
Сборщик проверяет UF2 family `0xADA52840`, номера и границы блоков, исключает
перекрытия и запись за пределами приложения `0x26000–0xED000` (815 104 байта).
Размер UF2-контейнера больше занимаемой flash-памяти; manifest отдельно
показывает оба размера и запас. Для MeshMemo проверяется встроенный build ID.

UF2 предназначен для совместимого загрузчика ProMicro/NiceNano и SoftDevice
S140 6.1.1, используемого официальным target. Проверка установленного загрузчика
остаётся обязательной перед прошивкой; сборщик его не заменяет и сам устройство
не прошивает. Планировщик обновления и восстановления по ESP32 flash-копии
для nRF52840 недоступен.

Полный профиль использует USF2 1/4, native USB CDC (transport 1), SHA-256 из
закреплённой rweather/Crypto 0.4.0. Требуется MeshMemo server 0.1.0a8+.
В `patches-only` USB-моста MeshMemo и встроенного build ID нет.

Статус программных проверок приведён в [validation.md](validation.md).
USB handshake/reconnect, радио, экран и сохранность настроек на физической
Faketec V4 пока не проверены.
