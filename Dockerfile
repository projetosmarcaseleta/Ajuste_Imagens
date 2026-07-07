FROM python:3.10-slim

# Instala dependências do sistema necessárias para OpenCV e processamento de imagens
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia os requisitos e instala
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do código
COPY . .

# Expõe a porta que será utilizada
EXPOSE 3003

# Cria as pastas de trabalho caso não existam
RUN mkdir -p uploads saida

# Inicia a aplicação usando gunicorn na porta 3003
# --preload: carrega AMBOS os modelos uma vez antes de fork, economizando memória via copy-on-write
# --timeout 600: tempo suficiente para carregar birefnet (~1GB) no boot
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:3003", "--timeout", "600", "--graceful-timeout", "30", "--preload", "app:app"]
