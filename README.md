# SAITULS

Личная коробка инструментов под Windows. Никакой глобальной цели — просто
сборник того, что накопилось за годы и реально используется каждый день:
контекстные меню Explorer, пара медиа-утилит, тихий «пищалка»-монитор для
медитаций. Всё это жило в разных папках, пока не собралось в один GUI с
галочками.

## Что внутри

`SAITULS.exe` — маленькое окошко в стиле Golden Default (тёмное, Verdan'а
без сглаживания, никаких скруглений). Четыре вкладки:

- **Menus** — 14 пунктов контекстного меню: копировать путь, взять во
  владение, удалить дубликаты / пустые / одинаковые / мусорные файлы,
  упаковать в папку, слить аудио, создать `_new_project`, показать скрытые
  файлы, PowerShell от админа, скачать с YouTube, FFmpeg-конвертация
  (23 расширения) и MKV-фиксы.
- **Monitor** — «blip»: тихий звуковой сигнал с интервалом, громкостью и
  автозапуском. Служит якорем для медитаций/пауз.
- **Tools** — быстрый запуск тех же скриптов без правого клика.
- **Settings** — корень тулкита, INI, автозапуск.

Плюс отдельно: лаунчеры OpenCode / Cline «YOLO здесь» с Wintage-профилем
консоли, каскадное меню Codex на 3 аккаунта, `problip/` — автономный
трей-монитор (C#), 33 локали для подписей меню.

## Запуск

```cmd
SAITULS_LAUNCHER.cmd
```

Лаунчер сам поднимает права (UAC) и стартует `SAITULS.exe`.

## Payload

Тяжёлые бинарники (ffmpeg, yt-dlp и т.д.) в git не лежат — репозиторий
только исходники. Возьми `SAITULS-payload-<версия>.zip` со страницы
Releases и распакуй в `Bin\` и `Bin\App\`, либо собери сам через
`BUILD_PAYLOAD.cmd`. Нужное:

- `Bin\FFMPEG.EXE`, `Bin\FFPROBE.EXE`, `Bin\yt-dlp.exe`, `Bin\ARIA2C.EXE`
- `Bin\App\ffmpeg.exe` + те же `FFPROBE`/`yt-dlp`/`ARIA2C`
- `Bin\App\AV1 CPU\FFMPEG.EXE`, `Bin\App\RTX40__\FFMPEG.EXE`
- `Bin\App\GS\` (Ghostscript), `Bin\App\ExifCleaner\`
- `Bin\App\AUDIO.EXE`, `VIDEO.EXE`, `ICON.ICO`, `YOUTUBE.INI`

## Сборка из исходников

```cmd
csc -nologo -target:winexe -out:SAITULS.exe -optimize+ -r:System.dll,System.Drawing.dll,System.Windows.Forms.dll SAITULS.cs
```

Нужен .NET Framework 4.8 (csc.exe идёт в комплекте).

## Тесты

```cmd
python tests\test_regs.py
```

Exit 0 = PASS. Проверяет reg-файлы, кодировки, паритет 33 локалей,
PowerShell-скрипты, YOLO-команды.

## Лицензия

MIT. Подробности в `LICENSE`.
