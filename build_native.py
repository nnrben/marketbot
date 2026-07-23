"""Компиляция движка бота в нативные .so (Cython) на этапе сборки образа.

Компилируются только модули с торговой логикой и проверкой лицензии — в них
нет интроспекции FastAPI/pydantic, поэтому Cython их не ломает. Слой
фреймворка (роутеры, модели, настройки, точка входа) остаётся исходником:
FastAPI и pydantic читают сигнатуры функций и поля моделей во время работы.

После компиляции .py и .c удаляются — в образ попадают только .so для этих
модулей. Это деттерент против копирования (нужен реверс машинного кода, а не
декомпиляция байткода), не замена лицензии.
"""
import os
import subprocess
import sys

COMPILE = [
    "app/license.py",
    "app/database.py",
    "app/services/grid_bot/instance.py",
    "app/services/grid_bot/service.py",
    "app/services/grid_bot/stats.py",
    "app/services/grid_bot/exchange_utils.py",
    "app/services/grid_bot/exceptions.py",
]


def main() -> None:
    missing = [f for f in COMPILE if not os.path.exists(f)]
    if missing:
        raise SystemExit(f"build_native: не найдены модули: {missing}")

    subprocess.check_call(["cythonize", "-3", "-i", "-q", *COMPILE])

    for f in COMPILE:
        for artifact in (f, f[:-3] + ".c"):
            if os.path.exists(artifact):
                os.remove(artifact)

    so_count = sum(
        1
        for root, _dirs, files in os.walk("app")
        for name in files
        if name.endswith(".so")
    )
    print(f"build_native: скомпилировано модулей={len(COMPILE)}, .so в дереве={so_count}")


if __name__ == "__main__":
    sys.exit(main())
