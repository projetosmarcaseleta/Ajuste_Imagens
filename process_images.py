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
import gc
import logging
import sys
import threading
import time
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
# Processamento de imagem — suporte a múltiplos modelos
# ---------------------------------------------------------------------------

# Modelos que ficam permanentemente em cache (leves)
_PERSISTENT_MODELS = {"padrao"}
# Modelos pesados: cache com TTL (carregam automaticamente, evictados após ociosidade)
_HEAVY_MODELS = {"preciso"}

# TTL em segundos: modelo pesado é mantido por este tempo após o último uso.
# Durante um lote, o TTL é renovado a cada imagem → modelo carregado apenas 1 vez.
HEAVY_MODEL_TTL = 60

_sessions: dict[str, object] = {}
_sessions_lock = threading.Lock()
_heavy_last_used: dict[str, float] = {}   # timestamp do último uso de cada modelo pesado

# Semáforo global: apenas 1 inferência rembg por vez.
# Impede que threads paralelas somem GBs de memória e matem o worker.
_inference_sem = threading.Semaphore(1)

# Tamanho máximo (lado maior) antes de entrar na inferência.
# 1500px: bom equilíbrio qualidade/memória para produtos e-commerce.
MAX_INFERENCE_SIZE = 1500


# Modelos disponíveis (chave interna → nome rembg)
AVAILABLE_MODELS = {
    "padrao":  "u2net",                # ~170MB, cache permanente
    "preciso": "birefnet-general-lite", # ~1GB+, cache TTL de 60s
}
DEFAULT_MODEL = "padrao"


def _load_session(model_key: str) -> object | None:
    """Carrega uma sessão rembg (sempre instancia nova, sem cache)."""
    model_name = AVAILABLE_MODELS[model_key]
    logger.info("Carregando modelo rembg: %s (%s)...", model_key, model_name)
    try:
        session = new_session(model_name)
        logger.info("Modelo '%s' carregado com sucesso.", model_key)
        return session
    except Exception as exc:
        logger.error("Erro ao carregar modelo %s: %s", model_name, exc)
        return None


def _heavy_model_evictor():
    """Thread daemon que verifica periodicamente se modelos pesados expiraram o TTL.
    
    Roda a cada 15s. Se um modelo pesado não foi usado nos últimos HEAVY_MODEL_TTL
    segundos, remove da cache e libera memória.
    """
    while True:
        time.sleep(15)
        try:
            now = time.time()
            to_evict = []
            with _sessions_lock:
                for key in list(_heavy_last_used.keys()):
                    idle = now - _heavy_last_used.get(key, 0)
                    if idle >= HEAVY_MODEL_TTL and key in _sessions:
                        to_evict.append(key)
                for key in to_evict:
                    del _sessions[key]
                    del _heavy_last_used[key]
            if to_evict:
                gc.collect()
                for key in to_evict:
                    logger.info("Modelo pesado '%s' removido da cache (ocioso por %ds+).", key, HEAVY_MODEL_TTL)
        except Exception as exc:
            logger.warning("Evictor: erro inesperado: %s", exc)


# Inicia o evictor como thread daemon (morre junto com o processo)
_evictor_thread = threading.Thread(target=_heavy_model_evictor, daemon=True, name="heavy-model-evictor")
_evictor_thread.start()


def get_rembg_session(model_key: str | None = None):
    """Retorna a sessão rembg para o modelo solicitado.
    
    - Modelos leves (u2net): cache permanente.
    - Modelos pesados (birefnet): cache com TTL. Carregado na primeira chamada,
      mantido enquanto há uso, evictado após HEAVY_MODEL_TTL segundos de ociosidade.
    """
    if model_key is None:
        model_key = DEFAULT_MODEL
    model_key = model_key if model_key in AVAILABLE_MODELS else DEFAULT_MODEL

    # Fast path sem lock
    if model_key in _sessions:
        if model_key in _HEAVY_MODELS:
            _heavy_last_used[model_key] = time.time()  # renova TTL
        return _sessions[model_key]

    with _sessions_lock:
        if model_key not in _sessions:
            session = _load_session(model_key)
            if session is None:
                return None
            _sessions[model_key] = session
            if model_key in _HEAVY_MODELS:
                _heavy_last_used[model_key] = time.time()
    return _sessions[model_key]


def preload_model():
    """Pré-carrega apenas o modelo leve (u2net) no boot.
    
    O birefnet (~1GB) NÃO é pré-carregado: com --preload do Gunicorn,
    o GC do Python invalida o COW durante o fork, duplicando a memória.
    O birefnet será carregado na primeira requisição e mantido por TTL.
    """
    logger.info("Pré-carregando modelo padrão: %s...", DEFAULT_MODEL)
    session = get_rembg_session(DEFAULT_MODEL)
    if session:
        logger.info("Modelo padrão '%s' pré-carregado com sucesso.", DEFAULT_MODEL)
    else:
        logger.warning("Falha ao pré-carregar modelo padrão '%s'.", DEFAULT_MODEL)
    gc.collect()


def _resize_if_needed(img: Image.Image, max_size: int = MAX_INFERENCE_SIZE) -> Image.Image:
    """Redimensiona proporcionalmente se o lado maior ultrapassar max_size."""
    w, h = img.size
    if max(w, h) <= max_size:
        return img
    scale = max_size / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    logger.debug("Redimensionando imagem: %dx%d → %dx%d", w, h, new_w, new_h)
    return img.resize((new_w, new_h), Image.LANCZOS)


def compose_on_white(image_bytes: bytes, model_key: str | None = None) -> Image.Image:
    """Remove o fundo e compõe sobre canvas branco. Retorna imagem RGB.

    Estratégia de memória:
    - u2net (padrão): cache permanente, leve (~170MB).
    - birefnet (preciso): cache TTL de {HEAVY_MODEL_TTL}s — reutilizado durante
      um lote inteiro; evictado automaticamente após ociosidade.
    - Semáforo garante apenas 1 inferência por vez.
    - Imagem redimensionada a {MAX_INFERENCE_SIZE}px para limitar pico de RAM.
    """
    if model_key is None or model_key not in AVAILABLE_MODELS:
        model_key = DEFAULT_MODEL

    # Redimensiona antes da inferência para reduzir pico de memória
    src_img = Image.open(io.BytesIO(image_bytes))
    src_img = _resize_if_needed(src_img)
    buf = io.BytesIO()
    src_img.save(buf, format="PNG")
    image_bytes_resized = buf.getvalue()
    src_img.close()
    del buf

    with _inference_sem:
        # get_rembg_session carrega se necessário e renova o TTL
        session = get_rembg_session(model_key)
        if session is None:
            logger.warning("Sessão '%s' indisponível; usando padrão.", model_key)
            session = get_rembg_session(DEFAULT_MODEL)

        output_bytes = remove(image_bytes_resized, session=session)

    del image_bytes_resized
    gc.collect()

    foreground = Image.open(io.BytesIO(output_bytes)).convert("RGBA")
    white_bg = Image.new("RGBA", foreground.size, (255, 255, 255, 255))
    white_bg.paste(foreground, mask=foreground.split()[3])
    result = white_bg.convert("RGB")
    foreground.close()
    white_bg.close()
    return result


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
