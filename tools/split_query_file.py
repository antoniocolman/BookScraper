import argparse
from pathlib import Path

def read_lines(p: Path) -> list[str]:
    raw = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    for line in raw:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--prefix", default=None)
    args = ap.parse_args()

    infile = Path(args.infile)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    lines = read_lines(infile)
    batch = max(1, args.batch)
    prefix = args.prefix or infile.stem

    nfiles = 0
    for i in range(0, len(lines), batch):
        nfiles += 1
        chunk = lines[i:i+batch]
        fn = outdir / f"{prefix}_{nfiles:04d}.txt"
        fn.write_text("\n".join(chunk) + "\n", encoding="utf-8")

    print(f"Total líneas: {len(lines)} | Archivos creados: {nfiles} | Outdir: {outdir}")

if __name__ == "__main__":
    main()
