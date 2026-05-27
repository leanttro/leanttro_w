#!/bin/bash
# Sobe nginx em background
nginx -g "daemon off;" &

# Sobe a API
uvicorn app:app --host 0.0.0.0 --port 8000
