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
import urllib.request
import urllib.error
from pathlib import Path
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    send_file,
    Response,
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

TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# ── Configurações AnyMarket / n8n ───────────────────────────────────────
N8N_HOST = 'api.marcaseleta.shop'
N8N_PORT = 80
N8N_PATH = '/webhook/background'
AM_HOST = 'api.anymarket.com.br'
SELF_BASE = 'https://app.marcaseleta.shop/background-remover'
CONCURRENCY = 5

# Fila de eventos SSE por job
job_events: dict[str, Queue] = {}
job_events_lock = threading.Lock()

# Controle de cancelamento de jobs
cancelled_jobs = set()
cancelled_jobs_lock = threading.Lock()

def is_job_cancelled(job_id):
    with cancelled_jobs_lock:
        return job_id in cancelled_jobs

def cancel_job(job_id):
    with cancelled_jobs_lock:
        cancelled_jobs.add(job_id)

class RateLimiter:
    def __init__(self, delay=0.110):
        self.delay = delay
        self.lock = threading.Lock()
        self.last_call = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_call
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self.last_call = time.time()

am_limiter = RateLimiter(0.110)

def emit_event(job_id, data):
    with job_events_lock:
        if job_id in job_events:
            job_events[job_id].put(data)

def _json_request(method, host, req_path, body=None, extra_headers=None, port=443):
    protocol = "http" if port == 80 else "https"
    url = f"{protocol}://{host}:{port}{req_path}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if extra_headers:
        headers.update(extra_headers)
    
    data = None
    if body:
        data = json.dumps(body).encode('utf-8')
    
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            resp_body = resp.read()
            try:
                return resp.status, json.loads(resp_body)
            except:
                return resp.status, {"_raw": resp_body.decode('utf-8')}
    except urllib.error.HTTPError as e:
        resp_body = e.read()
        try:
            return e.code, json.loads(resp_body)
        except:
            return e.code, {"_raw": resp_body.decode('utf-8')}
    except Exception as e:
        raise e

def _am_request(method, req_path, body, token):
    am_limiter.wait()
    return _json_request(method, AM_HOST, req_path, body, {"gumgaToken": token}, 443)

def _download_image(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read()


def _process_one_foto_am(job_id, foto, index, total, opts):
    src_url = foto.get("standard_url") or foto.get("original_url")
    is_main = str(foto.get("main_photo")) == '1' or foto.get("main_photo") is True
    idx = int(foto.get("product_photo_index", 0))
    variacao = foto.get("variacao")
    tem_var = str(foto.get("tem_variacao_visual", "false")).lower() in ('true', '1')
    
    label = f"[{index+1}/{total}] Foto {foto.get('id_foto')} — SKU {foto.get('sku', '—')}"
    if variacao:
        label += f" — Var: {variacao}"
        
    emit_event(job_id, {"event": "log", "tp": "info", "msg": f"   ⏳ {label}"})
    
    result = {
        "sku": foto.get("sku"), "id_produto": foto.get("id_produto"), "id_foto": foto.get("id_foto"),
        "variacao": variacao, "url_original": src_url, "nova_url": None, "status": "ERRO", "motivo_erro": None
    }
    
    token = opts["token"]
    delete_old = opts["deleteOld"]
    
    try:
        if is_job_cancelled(job_id):
            raise Exception("Cancelado pelo usuário")
            
        if not src_url:
            raise Exception("URL da imagem ausente")
            
        emit_event(job_id, {"event": "log", "tp": "info", "msg": f"   🎨 Removendo fundo..."})
        
        status, img_data = _download_image(src_url)
        if status != 200 or not img_data:
            raise Exception(f"Download falhou: HTTP {status}")
            
        if is_job_cancelled(job_id):
            raise Exception("Cancelado pelo usuário")
            
        result_img = compose_on_white(img_data)
        
        filename = f"bgrem_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
        filepath = TEMP_DIR / filename
        
        buf = io.BytesIO()
        result_img.save(buf, format="JPEG", quality=90)
        with open(filepath, "wb") as f:
            f.write(buf.getvalue())
            
        def _cleanup():
            time.sleep(600) # 10 min
            try:
                if filepath.exists():
                    filepath.unlink()
            except:
                pass
        threading.Thread(target=_cleanup, daemon=True).start()
        
        new_url = f"{SELF_BASE}/temp/{filename}"
        
        if is_job_cancelled(job_id):
            raise Exception("Cancelado pelo usuário")
            
        emit_event(job_id, {"event": "log", "tp": "info", "msg": f"   📤 Enviando nova foto ao AnyMarket... (URL: {new_url})"})
        
        post_body = {"url": new_url, "index": idx, "main": False}
        if tem_var and variacao:
            post_body["variation"] = variacao
            emit_event(job_id, {"event": "log", "tp": "info", "msg": f"   🏷️  Variação visual: {variacao}"})
            
        post_r_status = 500
        post_r_body = {}
        for attempt in range(1, 4):
            if is_job_cancelled(job_id):
                raise Exception("Cancelado pelo usuário")
            post_r_status, post_r_body = _am_request("POST", f"/v2/products/{foto['id_produto']}/images", post_body, token)
            if post_r_status < 400 and post_r_body.get("id"):
                break
            if attempt < 3:
                delay = 3 if post_r_status != 429 else 3 * attempt
                emit_event(job_id, {"event": "log", "tp": "skip", "msg": f"   ⚠️ POST falhou (HTTP {post_r_status}), aguardando {delay}s..."})
                time.sleep(delay)
                
        if post_r_status >= 400 or not post_r_body.get("id"):
            raise Exception(f"POST {post_r_status}: {str(post_r_body)[:200]}")
            
        new_photo_id = post_r_body["id"]
        result["nova_url"] = new_url
        
        if is_job_cancelled(job_id):
            raise Exception("Cancelado pelo usuário")
            
        emit_event(job_id, {"event": "log", "tp": "info", "msg": f"   🔢 Ajustando índice ({idx}) e main ({is_main})..."})
        try:
            put_body = {"id": int(new_photo_id), "index": idx, "main": is_main}
            if tem_var and variacao:
                put_body["variation"] = variacao
                
            put_r_status, put_r_body = _am_request("PUT", f"/v2/products/{foto['id_produto']}/images", put_body, token)
            if put_r_status == 429:
                emit_event(job_id, {"event": "log", "tp": "skip", "msg": f"   ⚠️ PUT Rate limit, aguardando..."})
                time.sleep(3)
                put_r_status, put_r_body = _am_request("PUT", f"/v2/products/{foto['id_produto']}/images", put_body, token)
            if put_r_status >= 400:
                raise Exception(f"HTTP {put_r_status}")
        except Exception as put_err:
            emit_event(job_id, {"event": "log", "tp": "skip", "msg": f"   ⚠️ PUT ignorado: {str(put_err)}"})
            
        if delete_old:
            if is_job_cancelled(job_id):
                raise Exception("Cancelado pelo usuário")
            emit_event(job_id, {"event": "log", "tp": "info", "msg": f"   🗑️  Removendo foto antiga..."})
            try:
                del_r_status, del_r_body = _am_request("DELETE", f"/v2/products/{foto['id_produto']}/images/{foto['id_foto']}", None, token)
                if del_r_status == 429:
                    emit_event(job_id, {"event": "log", "tp": "skip", "msg": f"   ⚠️ DELETE Rate limit, aguardando..."})
                    time.sleep(3)
                    del_r_status, del_r_body = _am_request("DELETE", f"/v2/products/{foto['id_produto']}/images/{foto['id_foto']}", None, token)
                if del_r_status >= 400 and del_r_status != 404:
                    raise Exception(f"HTTP {del_r_status}")
            except Exception as del_err:
                emit_event(job_id, {"event": "log", "tp": "skip", "msg": f"   ⚠️ DELETE ignorado: {str(del_err)}"})
                
        result["status"] = "SUCESSO"
        emit_event(job_id, {"event": "log", "tp": "ok", "msg": f"   ✅ Concluída!"})
        
    except Exception as err:
        if str(err) == "Cancelado pelo usuário":
            result["status"] = "CANCELADO"
            result["motivo_erro"] = str(err)
            emit_event(job_id, {"event": "log", "tp": "err", "msg": f"   🛑 Cancelado!"})
        else:
            result["motivo_erro"] = str(err)
            emit_event(job_id, {"event": "log", "tp": "err", "msg": f"   ❌ {str(err)}"})
        
    return result

def _process_am_job_worker(job_id, oi, skus, token, delete_old):
    try:
        if is_job_cancelled(job_id):
            emit_event(job_id, {"event": "error", "msg": "Cancelado pelo usuário"})
            return
            
        emit_event(job_id, {"event": "log", "tp": "info", "msg": "🔍 Consultando banco de dados via n8n..."})
        status, body = _json_request("POST", N8N_HOST, N8N_PATH, {"oi": oi, "skus": skus}, {}, N8N_PORT)
        
        if is_job_cancelled(job_id):
            emit_event(job_id, {"event": "error", "msg": "Cancelado pelo usuário"})
            return
            
        if status != 200 or not body.get("ok"):
            emit_event(job_id, {"event": "error", "msg": f"Falha n8n ({status}): {str(body)[:200]}"})
            return
            
        fotos = body.get("fotos", [])
        if not fotos:
            emit_event(job_id, {"event": "log", "tp": "skip", "msg": "⚠️ Nenhuma imagem encontrada."})
            emit_event(job_id, {"event": "complete", "total": 0, "ok": 0, "erros": 0, "results": []})
            return
            
        if is_job_cancelled(job_id):
            emit_event(job_id, {"event": "error", "msg": "Cancelado pelo usuário"})
            return
            
        emit_event(job_id, {"event": "log", "tp": "info", "msg": f"📸 {len(fotos)} foto(s) encontrada(s). Processando com {CONCURRENCY} workers..."})
        emit_event(job_id, {"event": "progress", "total": len(fotos), "done": 0, "ok": 0, "erros": 0})
        
        results = []
        done_count = 0
        
        opts = {"token": token, "deleteOld": delete_old}
        
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {
                executor.submit(_process_one_foto_am, job_id, f, i, len(fotos), opts): f 
                for i, f in enumerate(fotos)
            }
            
            for future in as_completed(futures):
                if is_job_cancelled(job_id):
                    for fut in futures:
                        fut.cancel()
                    break
                res = future.result()
                results.append(res)
                done_count += 1
                
                ok_n = sum(1 for r in results if r["status"] == "SUCESSO")
                err_n = len(results) - ok_n
                emit_event(job_id, {"event": "progress", "total": len(fotos), "done": done_count, "ok": ok_n, "erros": err_n})
                
        if is_job_cancelled(job_id):
            emit_event(job_id, {"event": "log", "tp": "err", "msg": "🛑 Processamento cancelado pelo usuário!"})
            emit_event(job_id, {"event": "error", "msg": "Cancelado pelo usuário"})
            return

        ok_total = sum(1 for r in results if r["status"] == "SUCESSO")
        err_total = len(results) - ok_total
        emit_event(job_id, {"event": "complete", "total": len(results), "ok": ok_total, "erros": err_total, "results": results})
        
    except Exception as err:
        emit_event(job_id, {"event": "error", "msg": f"Erro fatal: {str(err)}"})


jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ── Rotas de páginas ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")



# ── API: Processar AnyMarket / n8n ──────────────────────────────────────

@app.route("/api/processar", methods=["POST"])
def api_processar():
    try:
        body = request.get_json(force=True)
        oi = str(body.get("oi", "")).strip()
        token = str(body.get("token", "")).strip()
        delete_old = body.get("deleteOld", True)
        
        skus_raw = body.get("skus", [])
        if isinstance(skus_raw, str):
            skus = [s.strip() for s in skus_raw.replace(',', '\n').split('\n') if s.strip()]
        else:
            skus = [str(s).strip() for s in skus_raw if str(s).strip()]
            
        if not oi:
            return jsonify({"ok": False, "error": "Campo 'oi' é obrigatório"}), 400
        if not token:
            return jsonify({"ok": False, "error": "Campo 'token' (gumgaToken) é obrigatório"}), 400
            
        job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:5]}"
        
        with job_events_lock:
            job_events[job_id] = Queue()
            
        thread = threading.Thread(
            target=_process_am_job_worker,
            args=(job_id, oi, skus, token, delete_old),
            daemon=True
        )
        thread.start()
        
        return jsonify({"ok": True, "jobId": job_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/cancelar/<job_id>", methods=["POST"])
def api_cancelar(job_id):
    cancel_job(job_id)
    emit_event(job_id, {"event": "log", "tp": "err", "msg": "🛑 Cancelamento solicitado pelo usuário..."})
    return jsonify({"ok": True, "message": "Cancelamento solicitado"})

@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    with job_events_lock:
        if job_id not in job_events:
            return jsonify({"error": "Job não encontrado"}), 404
            
    q = job_events[job_id]
    
    def event_stream():
        yield ": connected\n\n"
        while True:
            try:
                data = q.get(timeout=15)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("event") in ("complete", "error", "done"):
                    break
            except Empty:
                yield ": heartbeat\n\n"
                
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/temp/<filename>")
def serve_temp(filename):
    if not (TEMP_DIR / filename).exists():
        return "Not found", 404
    return send_from_directory(TEMP_DIR, filename, mimetype="image/jpeg")




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





# ── Iniciar servidor ────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
