#!/bin/bash
set -e

# VidNugget startup script

if [ ! -f ".env" ]; then
  echo "⚠️  No .env file found. Copying from .env.example..."
  cp .env.example .env
  echo "📝 Edit .env and add your ANTHROPIC_API_KEY, then run this again."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "📦 Installing dependencies..."
pip install -q -r requirements.txt

echo ""
echo "🧠 Starting VidNugget..."
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
OB="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/VidNugget"
echo "   Local:          http://localhost:8000"
echo "   Mobile (Wi-Fi): http://${LOCAL_IP}:8000"
echo ""
echo "📥 Queue note:    $OB/Queue.md"
echo "   Share YouTube → Obsidian → Queue.md from anywhere"
echo ""
echo "📚 Nuggets:       $OB/knowledge_base/"
echo "   Browse in Obsidian on any device"
echo ""

python app.py
