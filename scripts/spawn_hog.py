"""Carico di prova per testare il rilevamento di TSlow: cpu | ram | leak | disk | hung.

Uso:
    .venv\\Scripts\\python scripts\\spawn_hog.py cpu --workers 8 --seconds 120
    .venv\\Scripts\\python scripts\\spawn_hog.py leak --rate 40 --minutes 12
    .venv\\Scripts\\python scripts\\spawn_hog.py hung --seconds 40
    .venv\\Scripts\\python scripts\\spawn_hog.py ram --mb 3000 --seconds 120
    .venv\\Scripts\\python scripts\\spawn_hog.py disk --workers 2 --seconds 60 --mb 32

Ctrl+C ferma il carico prima della scadenza. I processi worker (per cpu/disk) sono figli diretti di
questo script: se lanciato dal terminale integrato di VS Code, la loro identita' L1 ("carta
bianca") arriva risalendo l'albero fino a Code.exe, come previsto da protection.py.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import time


def _cpu_worker(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        x = 0
        for _ in range(200_000):
            x += 1


def cmd_cpu(args: argparse.Namespace) -> None:
    print(f"[spawn_hog] cpu: {args.workers} worker per {args.seconds}s (script PID {os.getpid()})")
    procs = [multiprocessing.Process(target=_cpu_worker, args=(args.seconds,)) for _ in range(args.workers)]
    for p in procs:
        p.start()
    print(f"[spawn_hog] cpu: PID dei worker: {[p.pid for p in procs]}")
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        print("[spawn_hog] cpu: interrotto, chiudo i worker...")
        for p in procs:
            p.terminate()
    print("[spawn_hog] cpu: terminato")


def cmd_ram(args: argparse.Namespace) -> None:
    print(f"[spawn_hog] ram: alloco {args.mb} MB e li mantengo per {args.seconds}s (PID {os.getpid()})")
    block = bytearray(args.mb * 1024 * 1024)
    for i in range(0, len(block), 4096):
        block[i] = 1  # forza il commit reale delle pagine, non solo la riserva di indirizzi
    print("[spawn_hog] ram: allocazione completata, in attesa...")
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    print("[spawn_hog] ram: terminato")


def cmd_leak(args: argparse.Namespace) -> None:
    print(f"[spawn_hog] leak: cresce di ~{args.rate} MB/min per {args.minutes} minuti (PID {os.getpid()})")
    chunks: list[bytearray] = []
    interval_s = 5.0
    mb_per_interval = args.rate * interval_s / 60.0
    end = time.monotonic() + args.minutes * 60
    try:
        while time.monotonic() < end:
            chunk = bytearray(int(mb_per_interval * 1024 * 1024))
            for i in range(0, len(chunk), 4096):
                chunk[i] = 1
            chunks.append(chunk)
            time.sleep(interval_s)
    except KeyboardInterrupt:
        pass
    print(f"[spawn_hog] leak: terminato, allocati ~{len(chunks) * mb_per_interval:.0f} MB")


def _disk_worker(seconds: float, mb_per_write: int, target_dir: str) -> None:
    path = os.path.join(target_dir, f"tslow_spawn_hog_{os.getpid()}.tmp")
    data = os.urandom(mb_per_write * 1024 * 1024)
    end = time.monotonic() + seconds
    try:
        with open(path, "wb") as f:
            while time.monotonic() < end:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def cmd_disk(args: argparse.Namespace) -> None:
    target_dir = args.dir or os.environ.get("TEMP", ".")
    print(f"[spawn_hog] disk: {args.workers} worker scrivono per {args.seconds}s in {target_dir}")
    procs = [
        multiprocessing.Process(target=_disk_worker, args=(args.seconds, args.mb, target_dir)) for _ in range(args.workers)
    ]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    print("[spawn_hog] disk: terminato")


def cmd_hung(args: argparse.Namespace) -> None:
    import tkinter as tk

    print(f"[spawn_hog] hung: apro una finestra e blocco il suo loop messaggi per {args.seconds}s (PID {os.getpid()})")
    root = tk.Tk()
    root.title("TSlow spawn_hog - finestra bloccata di proposito")
    root.geometry("420x150")
    tk.Label(
        root,
        text="Questa finestra e' bloccata di proposito\nper testare il rilevamento 'non risponde'.",
        padx=20,
        pady=20,
    ).pack(expand=True)
    root.update()
    print("[spawn_hog] hung: finestra visibile, busy-loop ora (niente piu' messaggi Windows processati)...")
    # Un time.sleep() qui non basterebbe: il detector richiede "non risponde" DA SOLO E il processo
    # che consuma risorse (>=25% CPU, RAM in crescita o I/O), per non confondere un'app bloccata da
    # debug/breakpoint (idle) con una davvero impazzita. Il busy-loop soddisfa entrambe le condizioni.
    end = time.monotonic() + args.seconds
    x = 0
    while time.monotonic() < end:
        x += 1
    print("[spawn_hog] hung: sblocco ed esco")
    root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Carico di prova per TSlow: cpu | ram | leak | disk | hung.")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_cpu = sub.add_parser("cpu", help="Satura la CPU con N worker in busy-loop.")
    p_cpu.add_argument("--workers", type=int, default=8)
    p_cpu.add_argument("--seconds", type=float, default=120)
    p_cpu.set_defaults(func=cmd_cpu)

    p_ram = sub.add_parser("ram", help="Alloca e mantiene un blocco di RAM.")
    p_ram.add_argument("--mb", type=int, default=2000)
    p_ram.add_argument("--seconds", type=float, default=120)
    p_ram.set_defaults(func=cmd_ram)

    p_leak = sub.add_parser("leak", help="Fa crescere la RAM privata in modo costante (simula un leak).")
    p_leak.add_argument("--rate", type=float, default=40, help="MB al minuto")
    p_leak.add_argument("--minutes", type=float, default=12)
    p_leak.set_defaults(func=cmd_leak)

    p_disk = sub.add_parser("disk", help="Scrive su disco in continuazione con N worker.")
    p_disk.add_argument("--workers", type=int, default=2)
    p_disk.add_argument("--seconds", type=float, default=60)
    p_disk.add_argument("--mb", type=int, default=32, help="MB per scrittura")
    p_disk.add_argument("--dir", type=str, default=None, help="Cartella di destinazione (default: %%TEMP%%)")
    p_disk.set_defaults(func=cmd_disk)

    p_hung = sub.add_parser("hung", help="Apre una finestra e ne blocca il loop messaggi (simula 'non risponde').")
    p_hung.add_argument("--seconds", type=float, default=40)
    p_hung.set_defaults(func=cmd_hung)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("spawn_hog.py e' pensato per Windows.", file=sys.stderr)
        sys.exit(1)
    main()
