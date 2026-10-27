#!/usr/bin/env python3
"""Tum test_*.py dosyalarini ayri sureclerde calistirir. Cikis kodu: basarisiz test sayisi."""
import glob
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
only = sys.argv[1:]
files = sorted(glob.glob(os.path.join(here, "test_*.py")))
if only:
    files = [f for f in files if any(o in os.path.basename(f) for o in only)]
failed = []
for path in files:
    t0 = time.monotonic()
    res = subprocess.run([sys.executable, path], capture_output=True, text=True)
    ok = res.returncode == 0
    print(f"{'OK  ' if ok else 'FAIL'} {os.path.basename(path)}  ({time.monotonic() - t0:.1f}s)")
    if not ok:
        failed.append(path)
        print((res.stdout + res.stderr)[-2500:])
print(f"\n{len(files) - len(failed)}/{len(files)} test dosyası geçti")
sys.exit(len(failed))
