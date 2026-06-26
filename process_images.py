"""
process_images.py
Substitui o fundo de imagens de produtos por branco puro (#FFFFFF).

Suporta dois modos de entrada:
  1. Pasta local com imagens  (-i ./pasta)
  2. Planilha (.xlsx ou .csv) com colunas "IMAGEM 1" até "IMAGEM 10"  (-s planilha.xlsx)

Uso — pasta local:
    python process_images.py -i ./entrada -o ./saida

Uso — planilha:
    python process_images.py -s produtos.xlsx -o ./saida
    python process_images.py -s produtos.xlsx -o ./saida --id-col "SKU"

    Com --id-col, os arquivos são nomeados como:  <SKU>_IMAGEM1.jpg
    Sem --id-col, o nome é extraído da própria URL.
"""

import argparse
import io
import logging
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from rembg import remove, new_session
from tqdm import tqdm

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Colunas de imagem fixas na planilha
IMAGE_COLUMNS = [f"IMAGEM {i}" for i in range(1, 11)]  # IMAGEM 1 … IMAGEM 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Processamento de imagem
# ---------------------------------------------------------------------------

_session = None

def get_rembg_session():
    global _session
    if _session is None:
        model_name = "isnet-general-use"
        logger.info("Carregando modelo rembg: %s...", model_name)
        try:
            _session = new_session(model_name)
        except Exception as exc:
            logger.error("Erro ao carregar modelo %s: %s. Usando padrao.", model_name, exc)
            _session = None
    return _session

def compose_on_white(image_bytes: bytes) -> Image.Image:
    """Remove o fundo e compõe sobre canvas branco. Retorna imagem RGB."""
    session = get_rembg_session()
    output_bytes = remove(image_bytes, session=session)
    foreground = Image.open(io.BytesIO(output_bytes)).convert("RGBA")
    white_bg = Image.new("RGBA", foreground.size, (255, 255, 255, 255))
    white_bg.paste(foreground, mask=foreground.split()[3])
    return white_bg.convert("RGB")


def save_image(img: Image.Image, output_path: Path) -> None:
    suffix = output_path.suffix.lower()
    fmt = "JPEG" if suffix in {".jpg", ".jpeg"} else suffix.lstrip(".").upper()
    if fmt == "WEBP":
        img.save(output_path, format="WEBP", quality=95)
    else:
        img.save(output_path, format=fmt, quality=95)


# ---------------------------------------------------------------------------
# Modo 1 — pasta local
# ---------------------------------------------------------------------------

def process_from_file(input_path: Path, output_path: Path) -> tuple[str, bool, str]:
    try:
        with open(input_path, "rb") as f:
            data = f.read()
        result = compose_on_white(data)
        save_image(result, output_path)
        return input_path.name, True, ""
    except Exception as exc:
        return input_path.name, False, str(exc)


def collect_images(input_dir: Path) -> list[Path]:
    return sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )


# ---------------------------------------------------------------------------
# Modo 2 — planilha
# ---------------------------------------------------------------------------

def filename_from_url(url: str) -> str:
    """Extrai nome do arquivo da URL; garante extensão suportada."""
    name = Path(urlparse(url).path).name.split("?")[0]
    if not name or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        name = (name or "image") + ".jpg"
    return name


def process_from_url(url: str, output_path: Path) -> tuple[str, bool, str]:
    label = output_path.name
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        result = compose_on_white(data)
        save_image(result, output_path)
        return label, True, ""
    except Exception as exc:
        return label, False, str(exc)


def load_spreadsheet(path: Path, id_col: str | None) -> list[tuple[str, str]]:
    """
    Lê a planilha e retorna lista de (url, nome_arquivo) para todas as
    colunas IMAGEM 1 … IMAGEM 10 que contenham URLs válidas.
    """
    if not PANDAS_AVAILABLE:
        logger.error("pandas não instalado. Rode: python -m pip install pandas openpyxl")
        sys.exit(1)

    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path, dtype=str)
    elif suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(path, dtype=str)
    else:
        logger.error("Formato de planilha não suportado: %s", suffix)
        sys.exit(1)

    # Quais colunas de imagem existem nesta planilha
    present_cols = [c for c in IMAGE_COLUMNS if c in df.columns]
    if not present_cols:
        logger.error(
            "Nenhuma coluna 'IMAGEM 1'–'IMAGEM 10' encontrada. "
            "Colunas disponíveis: %s", list(df.columns)
        )
        sys.exit(1)

    logger.info("Colunas de imagem encontradas: %s", present_cols)

    if id_col and id_col not in df.columns:
        logger.error(
            "Coluna de ID '%s' não encontrada. Colunas disponíveis: %s",
            id_col, list(df.columns)
        )
        sys.exit(1)

    rows: list[tuple[str, str]] = []
    for row_idx, row in df.iterrows():
        # Identificador da linha (para nomear a pasta)
        row_id = str(row[id_col]).strip() if id_col else None

        img_counter = 0  # contador sequencial por produto
        for col in present_cols:
            url = str(row.get(col, "")).strip()
            if not url or url.lower() in {"nan", "none", ""}:
                continue

            img_counter += 1
            ext = Path(urlparse(url).path).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                ext = ".jpg"

            if row_id:
                # Estrutura: <output>/<SKU>/<SKU>_imagem1.jpg
                relative = f"{row_id}/{row_id}_imagem{img_counter}{ext}"
            else:
                # Sem ID: flat, usa nome da URL com sufixo sequencial
                base_stem = Path(filename_from_url(url)).stem
                relative = f"{base_stem}_imagem{img_counter}{ext}"

            rows.append((url, relative))

    return rows


# ---------------------------------------------------------------------------
# Runner genérico
# ---------------------------------------------------------------------------

def run_batch(tasks: list[tuple], workers: int, mode: str) -> None:
    success_count = 0
    error_count = 0
    errors: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        if mode == "file":
            futures = {
                executor.submit(process_from_file, src, dst): dst.name
                for src, dst in tasks
            }
        else:
            futures = {
                executor.submit(process_from_url, src, dst): dst.name
                for src, dst in tasks
            }

        with tqdm(total=len(futures), unit="img", desc="Processando") as pbar:
            for future in as_completed(futures):
                label, ok, err_msg = future.result()
                if ok:
                    success_count += 1
                else:
                    error_count += 1
                    errors.append((str(label), err_msg))
                    logger.error("Erro em '%s': %s", label, err_msg)
                pbar.update(1)

    logger.info("Concluído. Sucesso: %d | Erros: %d", success_count, error_count)
    if errors:
        logger.warning("Arquivos com erro:")
        for name, msg in errors:
            logger.warning("  • %s — %s", name, msg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Substitui o fundo de imagens de produtos por branco puro (#FFFFFF).",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "-i", "--input",
        type=Path,
        metavar="PASTA",
        help="Pasta com imagens locais (JPG, PNG, WEBP).",
    )
    source.add_argument(
        "-s", "--spreadsheet",
        type=Path,
        metavar="PLANILHA",
        help="Planilha .xlsx ou .csv com colunas 'IMAGEM 1' até 'IMAGEM 10'.",
    )

    parser.add_argument(
        "-o", "--output",
        required=True,
        type=Path,
        metavar="PASTA",
        help="Pasta de destino para as imagens processadas.",
    )
    parser.add_argument(
        "--id-col",
        default=None,
        metavar="COLUNA",
        help=(
            "Coluna usada como identificador para nomear os arquivos.\n"
            "Ex: --id-col SKU  →  SKU123_IMAGEM1.jpg\n"
            "Se omitido, o nome é extraído da URL."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Número de threads paralelas (padrão: 4).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.input:
        if not args.input.exists() or not args.input.is_dir():
            logger.error("Pasta de entrada não encontrada: %s", args.input)
            sys.exit(1)
        images = collect_images(args.input)
        if not images:
            logger.warning("Nenhuma imagem encontrada em: %s", args.input)
            sys.exit(0)
        logger.info("%d imagem(ns) encontrada(s). Threads: %d", len(images), args.workers)
        tasks = [(img, output_dir / img.name) for img in images]
        run_batch(tasks, args.workers, mode="file")

    else:
        if not args.spreadsheet.exists():
            logger.error("Planilha não encontrada: %s", args.spreadsheet)
            sys.exit(1)
        rows = load_spreadsheet(args.spreadsheet, args.id_col)
        if not rows:
            logger.warning("Nenhuma URL válida encontrada na planilha.")
            sys.exit(0)
        logger.info("%d URL(s) encontrada(s). Threads: %d", len(rows), args.workers)
        tasks = []
        for url, relative in rows:
            dest = output_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            tasks.append((url, dest))
        run_batch(tasks, args.workers, mode="url")


if __name__ == "__main__":
    main()
