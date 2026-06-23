import re

with open(r"templates\index.html", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add Tab Button
tab_btn = """            <button class="tab-btn" data-tab="gallery" id="tab-btn-gallery">
                <span class="tab-icon">🗂️</span>
                Galeria
            </button>
            <button class="tab-btn" data-tab="anymarket" id="tab-btn-anymarket">
                <span class="tab-icon">🛒</span>
                AnyMarket
            </button>"""
content = re.sub(r'<button class="tab-btn" data-tab="gallery" id="tab-btn-gallery">.*?Galeria\s*</button>', tab_btn, content, flags=re.DOTALL)

# 2. Add Tab Content
tab_content = """        <!-- ══════════════════════════════════════════════════════
             TAB 5 — AnyMarket
             ══════════════════════════════════════════════════════ -->
        <div class="tab-content" id="tab-anymarket">
            <div class="section-header">
                <h2>🛒 Integração AnyMarket (n8n)</h2>
                <p>Processa imagens diretamente do AnyMarket, removendo o fundo e enviando as novas versões.</p>
            </div>
            
            <div class="form-grid">
                <div class="form-group" style="grid-column: span 2;">
                    <label class="form-label">OI</label>
                    <input type="text" class="form-input" id="am-oi-input" placeholder="Ex: DB1">
                </div>
                <div class="form-group" style="grid-column: span 2;">
                    <label class="form-label">SKUs (separados por vírgula ou quebra de linha)</label>
                    <textarea class="form-input" id="am-skus-input" rows="3" placeholder="SKU1, SKU2..."></textarea>
                </div>
                <div class="form-group" style="grid-column: span 2;">
                    <label class="form-label">Token do AnyMarket (gumgaToken)</label>
                    <input type="password" class="form-input" id="am-token-input" placeholder="Token da API do AnyMarket">
                </div>
            </div>
            
            <div class="mt-24" style="margin-bottom: 12px;">
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                    <input type="checkbox" id="am-delete-old" checked>
                    <span>Remover imagens originais do AnyMarket após processamento</span>
                </label>
            </div>

            <div class="mt-24">
                <button class="btn btn-primary" id="am-process-btn">
                    🚀 Iniciar Processamento
                </button>
            </div>

            <div class="progress-container" id="am-progress" style="display: none; margin-top: 24px;">
                <div class="progress-header">
                    <h4><div class="spinner"></div> Sincronizando…</h4>
                    <span id="am-progress-text">0 / 0</span>
                </div>
                <div class="progress-bar-track">
                    <div class="progress-bar-fill" id="am-progress-bar"></div>
                </div>
                <div class="progress-status" id="am-progress-status">Iniciando…</div>
                
                <div class="log-wrap" id="am-log" style="max-height: 250px; overflow-y: auto; background: var(--surface); padding: 12px; border-radius: 8px; margin-top: 16px; font-family: monospace; font-size: 12px;">
                </div>
            </div>
        </div>
    </div>"""

content = content.replace("    </div>\n\n    <!-- ── Modal de Detalhes", tab_content + "\n\n    <!-- ── Modal de Detalhes")

# 3. Add JS Logic
js_logic = """
    // ═══════════════════════════════════════════════════════════════
    // TAB 5 — AnyMarket
    // ═══════════════════════════════════════════════════════════════
    
    const amProcessBtn = document.getElementById('am-process-btn');
    const amLog = document.getElementById('am-log');
    let amEventSource = null;

    if (localStorage.getItem('am_oi')) document.getElementById('am-oi-input').value = localStorage.getItem('am_oi');
    if (localStorage.getItem('am_token')) document.getElementById('am-token-input').value = localStorage.getItem('am_token');
    
    amProcessBtn.addEventListener('click', async () => {
        const oi = document.getElementById('am-oi-input').value.trim();
        const skus = document.getElementById('am-skus-input').value;
        const token = document.getElementById('am-token-input').value.trim();
        const deleteOld = document.getElementById('am-delete-old').checked;

        if (!oi || !token) {
            showToast('OI e Token são obrigatórios', 'error');
            return;
        }

        localStorage.setItem('am_oi', oi);
        localStorage.setItem('am_token', token);

        const payload = { oi, skus, token, deleteOld };
        
        amProcessBtn.disabled = true;
        amLog.innerHTML = '';
        document.getElementById('am-progress').style.display = 'block';
        document.getElementById('am-progress').classList.add('active');
        document.getElementById('am-progress-bar').style.width = '0%';
        document.getElementById('am-progress-text').textContent = '0 / 0';
        document.getElementById('am-progress-status').textContent = 'Iniciando...';
        
        try {
            const resp = await fetch('/api/processar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await resp.json();
            
            if (!resp.ok) throw new Error(data.error);
            
            showToast('Processamento no AnyMarket iniciado!', 'info');
            startAMProgress(data.jobId);
        } catch (e) {
            showToast('Erro: ' + e.message, 'error');
            amProcessBtn.disabled = false;
        }
    });

    function addAMLog(msg, type) {
        const div = document.createElement('div');
        div.className = `log-item ${type || ''}`;
        div.style.marginBottom = '4px';
        
        let color = '#ccc';
        if (type === 'err') color = '#ff4444';
        if (type === 'ok') color = '#00C851';
        if (type === 'skip') color = '#ffbb33';
        if (type === 'info') color = '#33b5e5';
        
        div.innerHTML = `<span style="color: ${color}">${msg}</span>`;
        amLog.appendChild(div);
        amLog.scrollTop = amLog.scrollHeight;
    }

    function startAMProgress(jobId) {
        if (amEventSource) amEventSource.close();
        
        amEventSource = new EventSource(`/api/progress/${jobId}`);
        
        amEventSource.onmessage = (e) => {
            if (e.data.startsWith(':')) return;
            
            try {
                const data = JSON.parse(e.data);
                
                if (data.event === 'log') {
                    addAMLog(data.msg, data.tp);
                } else if (data.event === 'progress') {
                    const pct = data.total > 0 ? Math.round((data.done / data.total) * 100) : 0;
                    document.getElementById('am-progress-bar').style.width = `${pct}%`;
                    document.getElementById('am-progress-text').textContent = `${data.done} / ${data.total}`;
                    document.getElementById('am-progress-status').textContent = `Processando... ${pct}% (OK: ${data.ok}, Erros: ${data.erros})`;
                } else if (data.event === 'complete') {
                    document.getElementById('am-progress-status').textContent = `✅ Concluído! (Total: ${data.total}, OK: ${data.ok}, Erros: ${data.erros})`;
                    amEventSource.close();
                    amProcessBtn.disabled = false;
                    showToast('Processamento AnyMarket finalizado!', 'success');
                } else if (data.event === 'error') {
                    addAMLog(`Erro: ${data.msg}`, 'err');
                    document.getElementById('am-progress-status').textContent = `❌ Erro: ${data.msg}`;
                    amEventSource.close();
                    amProcessBtn.disabled = false;
                }
            } catch (err) {
                console.error("Erro no SSE:", err);
            }
        };
        
        amEventSource.onerror = () => {
            amEventSource.close();
            amProcessBtn.disabled = false;
        };
    }

    // ── Product Modal ───────────────────────────────────────────"""

content = content.replace("    // ── Product Modal ───────────────────────────────────────────", js_logic)

with open(r"templates\index.html", "w", encoding="utf-8") as f:
    f.write(content)

print("Updated index.html successfully!")
