"""Поддельные cjxl и djxl для тестов: «JPEG XL» здесь – это zlib с заголовком FJXL.

Особые случаи для проверки запасных вариантов упаковки:
  данные начинаются с BROKEN – cjxl завершается с ошибкой;
  данные содержат MISMATCH – djxl восстанавливает файл с лишним байтом.
"""

import sys
import zlib


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    if "--version" in args:
        print(f"{tool} fake v0.0")
        return 0
    src, dst = args[0], args[1]
    data = open(src, "rb").read()
    if tool == "cjxl":
        if data.startswith(b"BROKEN"):
            print("cjxl: unsupported JPEG", file=sys.stderr)
            return 1
        open(dst, "wb").write(b"FJXL" + zlib.compress(data, 9))
        return 0
    if not data.startswith(b"FJXL"):
        print("djxl: not a JPEG XL file", file=sys.stderr)
        return 1
    out = zlib.decompress(data[4:])
    if b"MISMATCH" in out:
        out += b"x"
    open(dst, "wb").write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
