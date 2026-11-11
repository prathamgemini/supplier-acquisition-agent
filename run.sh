#!/usr/bin/env bash
# Starts the dashboard. Press Ctrl+C to stop.
cd "$(dirname "$0")"
.venv/bin/python -m streamlit run app.py
