FROM python:3.11-slim

# Instala nginx
RUN apt-get update && apt-get install -y nginx && rm -rf /var/lib/apt/lists/*

# Instala dependências Python
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ .

# Copia frontend pro nginx
COPY frontend/ /usr/share/nginx/html

# Script de inicialização
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 80 8000

CMD ["/start.sh"]
