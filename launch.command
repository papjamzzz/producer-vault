#!/bin/bash
cd "$(dirname "$0")"
echo "Starting ProducerVault on http://127.0.0.1:5565"
python3 app.py
