import argparse
from pathlib import Path

import pypdfium2 as pdfium


def rasterize(pdf_path: Path, output_dir: Path, dpi: int = 150) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(pdf_path)
    scale = dpi / 72
    paths: list[Path] = []
    for index in range(len(pdf)):
        page = pdf[index]
        image = page.render(scale=scale).to_pil()
        path = output_dir / f"page-{index + 1}.png"
        image.save(path)
        paths.append(path)
        page.close()
    pdf.close()
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Render each PDF page to a PNG image.")
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()
    for path in rasterize(args.pdf_path, args.output_dir, args.dpi):
        print(path.resolve())


if __name__ == "__main__":
    main()
