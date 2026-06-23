"""
app.py — Backend Flask para o processamento de imagens com remoção de fundo.
Expõe uma interface web para upload de imagens/planilhas e visualização dos resultados.
"""

import io
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    send_file,
)

from process_images import (
    compose_on_white,
    save_image,
    collect_images,
    load_spreadsheet,
    process_from_file,
    process_from_url,
    SUPPORTED_EXTENSIONS,
)

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "saida"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ── Estado global dos jobs ──────────────────────────────────────────────
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ── Rotas de páginas ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── API: listar produtos já processados ─────────────────────────────────

@app.route("/api/products")
def list_products():
    """Retorna lista de pastas/produtos na pasta de saída."""
    if not OUTPUT_DIR.exists():
        return jsonify([])

    products = []
    search = request.args.get("search", "").strip().lower()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 30))

    all_dirs = sorted(
        [d for d in OUTPUT_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )

    if search:
        all_dirs = [d for d in all_dirs if search in d.name.lower()]

    total = len(all_dirs)
    start = (page - 1) * per_page
    end = start + per_page
    page_dirs = all_dirs[start:end]

    for d in page_dirs:
        images = sorted(
            [f.name for f in d.iterdir()
             if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
        )
        if images:
            products.append({
                "id": d.name,
                "images": images,
                "count": len(images),
            })

    return jsonify({
        "products": products,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
    })


# ── API: servir imagem processada ───────────────────────────────────────

@app.route("/api/image/<product_id>/<filename>")
def serve_image(product_id, filename):
    folder = OUTPUT_DIR / product_id
    if not folder.exists():
        return jsonify({"error": "Produto não encontrado"}), 404
    return send_from_directory(folder, filename)


# ── API: processar imagens enviadas (upload direto) ─────────────────────

@app.route("/api/upload-images", methods=["POST"])
def upload_images():
    """Recebe imagens via upload e processa em background."""
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "Nenhuma imagem enviada"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in files:
        if f.filename and Path(f.filename).suffix.lower() in SUPPORTED_EXTENSIONS:
            safe_name = Path(f.filename).name
            dest = job_upload_dir / safe_name
            f.save(dest)
            saved.append(dest)

    if not saved:
        return jsonify({"error": "Nenhuma imagem válida enviada"}), 400

    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "type": "images",
            "status": "processing",
            "total": len(saved),
            "done": 0,
            "errors": [],
            "started_at": time.time(),
        }

    thread = threading.Thread(
        target=_process_images_job,
        args=(job_id, saved),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "total": len(saved)})


def _process_images_job(job_id: str, images: list[Path]):
    job_output = OUTPUT_DIR / f"upload_{job_id}"
    job_output.mkdir(parents=True, exist_ok=True)

    for img_path in images:
        out_path = job_output / img_path.name
        name, ok, err = process_from_file(img_path, out_path)
        with jobs_lock:
            jobs[job_id]["done"] += 1
            if not ok:
                jobs[job_id]["errors"].append({"file": name, "error": err})

    with jobs_lock:
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["output_folder"] = f"upload_{job_id}"

    # Limpar uploads
    upload_dir = UPLOAD_DIR / job_id
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)


# ── API: processar planilha ─────────────────────────────────────────────

@app.route("/api/upload-spreadsheet", methods=["POST"])
def upload_spreadsheet():
    """Recebe planilha e processa URLs em background."""
    file = request.files.get("spreadsheet")
    if not file:
        return jsonify({"error": "Nenhuma planilha enviada"}), 400

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}:
        return jsonify({"error": "Formato não suportado. Use .xlsx, .xls ou .csv"}), 400

    job_id = str(uuid.uuid4())[:8]
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)

    spreadsheet_path = job_upload_dir / f"planilha{suffix}"
    file.save(spreadsheet_path)

    id_col = request.form.get("id_col", "").strip() or None
    workers = int(request.form.get("workers", 4))

    # Ler planilha para contar URLs
    try:
        rows = load_spreadsheet(spreadsheet_path, id_col)
    except SystemExit:
        return jsonify({"error": "Erro ao ler a planilha. Verifique as colunas."}), 400

    if not rows:
        return jsonify({"error": "Nenhuma URL válida encontrada na planilha."}), 400

    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "type": "spreadsheet",
            "status": "processing",
            "total": len(rows),
            "done": 0,
            "errors": [],
            "started_at": time.time(),
        }

    thread = threading.Thread(
        target=_process_spreadsheet_job,
        args=(job_id, rows, workers),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "total": len(rows)})


def _process_spreadsheet_job(job_id: str, rows: list[tuple[str, str]], workers: int):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    for url, relative in rows:
        dest = OUTPUT_DIR / relative
        dest.parent.mkdir(parents=True, exist_ok=True)

        label, ok, err = process_from_url(url, dest)
        with jobs_lock:
            jobs[job_id]["done"] += 1
            if not ok:
                jobs[job_id]["errors"].append({"file": label, "error": err})

    with jobs_lock:
        jobs[job_id]["status"] = "completed"

    # Limpar uploads
    upload_dir = UPLOAD_DIR / job_id
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)


# ── API: processar imagem única (preview rápido) ────────────────────────

@app.route("/api/preview", methods=["POST"])
def preview_image():
    """Processa uma única imagem e retorna o resultado inline."""
    file = request.files.get("image")
    if not file:
        return jsonify({"error": "Nenhuma imagem enviada"}), 400

    try:
        data = file.read()
        result = compose_on_white(data)

        buf = io.BytesIO()
        result.save(buf, format="JPEG", quality=95)
        buf.seek(0)

        return send_file(buf, mimetype="image/jpeg", download_name="preview.jpg")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── API: status do job ──────────────────────────────────────────────────

@app.route("/api/job/<job_id>")
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    return jsonify(job)


# ── API: estatísticas ───────────────────────────────────────────────────

@app.route("/api/stats")
def stats():
    if not OUTPUT_DIR.exists():
        return jsonify({"total_products": 0, "total_images": 0})

    total_products = 0
    total_images = 0
    for d in OUTPUT_DIR.iterdir():
        if d.is_dir():
            imgs = [f for f in d.iterdir()
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
            if imgs:
                total_products += 1
                total_images += len(imgs)

    return jsonify({
        "total_products": total_products,
        "total_images": total_images,
    })


# ── API: download de todas as imagens de um produto como ZIP ────────────

@app.route("/api/download/<product_id>")
def download_product(product_id):
    folder = OUTPUT_DIR / product_id
    if not folder.exists():
        return jsonify({"error": "Produto não encontrado"}), 404

    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.iterdir():
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                zf.write(f, f.name)

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        download_name=f"{product_id}.zip",
        as_attachment=True,
    )


# ── Iniciar servidor ────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
