import os
import subprocess

folder = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
main_png = os.path.join(folder, "main1.png")

if not os.path.exists(main_png):
    raise FileNotFoundError(f"Не найден {main_png}")

for f in os.listdir(folder):
    if f.lower().endswith(".dds"):
        dds_path = os.path.join(folder, f)
        temp_dds = os.path.join(folder, "temp_main.dds")
        if os.path.exists(temp_dds):
            os.remove(temp_dds)

        # Конвертируем через texconv точно как Paint.NET
        subprocess.run([
            "texconv.exe",
            "-f", "R8G8B8A8_UNORM_SRGB",  # DX10+ sRGB
            "-y",                          # перезаписать если есть
            "-srgb",                       # критично для Paint.NET цвета
            "-o", folder,
            "-nologo",
            main_png
        ], check=True)

        generated_dds = os.path.join(folder, "main1.dds")
        if os.path.exists(dds_path):
            os.remove(dds_path)
        os.rename(generated_dds, dds_path)

        print(f"Заменил: {dds_path}")

print("Все DDS заменены на main1.png в точном формате R8G8B8A8 sRGB DX10+!")
