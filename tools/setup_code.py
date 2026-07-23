from __future__ import annotations

import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import CONFIG_PATH, create_config


def main() -> None:
    print("\nFC26 AI CHARACTER STUDIO — CONFIGURATION PRIVÉE\n")
    if CONFIG_PATH.exists():
        answer = input("Un code existe déjà. Le remplacer ? (o/N) : ").strip().lower()
        if answer != "o":
            print("Aucun changement.")
            return
    while True:
        first = getpass.getpass("Nouveau code d’accès (6 caractères minimum) : ")
        second = getpass.getpass("Confirme le code : ")
        if first != second:
            print("Les codes ne correspondent pas.\n")
            continue
        try:
            create_config(first)
        except ValueError as exc:
            print(f"{exc}\n")
            continue
        print("\nCode enregistré. Le Studio écoute uniquement sur ce PC.")
        print("Adresse : http://127.0.0.1:8765")
        return


if __name__ == "__main__":
    main()
