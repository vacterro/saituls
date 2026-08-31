# SAITULS — личные тулзы под Windows

Сборник того, что накопилось за годы и реально используется каждый день:
контекстные меню Explorer, медиа-утилиты, тихий blip-монитор. Без пафоса,
без глобальной цели — просто рабочие инструменты в одном окне.

## Быстрый старт

```cmd
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Скрипт сам поднимет права, скачает Python (если нет), ffmpeg, yt-dlp,
aria2c — и установит всё меню. Потом можно запускать `SAITULS_LAUNCHER.cmd`.

## SAITULS.exe — четыре вкладки

- **Menus** — 14 пунктов контекстного меню с галочками
- **Monitor** — blip-пищалка с интервалом и громкостью
- **Tools** — запуск мини-тулз (DL_YT, DEL_*, Pack, Project)
- **Settings** — путь, автозапуск

## Что внутри

- 14 контекстных меню (копировать путь, взять владение, удалить дубликаты/
  пустые/одинаковые/мусор, упаковать, слить аудио, новый проект, скрытые
  файлы, PS от админа, YouTube, FFmpeg/MKV)
- OpenCode / Cline YOLO здесь
- Codex на 3 аккаунта
- 33 локали для подписей меню
- Problip — отдельный blip-монитор в трее

## Сборка

Если нужен свежий `SAITULS.exe`:

```cmd
csc -nologo -target:winexe -out:SAITULS.exe -optimize+ \
  -r:System.dll,System.Drawing.dll,System.Windows.Forms.dll \
  SAITULS.cs
```

## Тесты

```cmd
python tests\test_regs.py
```

## Лицензия

MIT. Подробнее в `LICENSE`.