#!/bin/bash
# MallPlus Dashboard — Startup Script
cd "$(dirname "$0")"

PORT=8080
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "192.168.1.71")

echo ""
echo "🛍️  MallPlus Launch Dashboard"
echo "══════════════════════════════"

# Kill any existing instance on port 8080
lsof -ti:$PORT | xargs kill -9 2>/dev/null && echo "♻️  Stopped previous instance on :$PORT"

echo "🚀  Starting server..."
python3 server.py &
SERVER_PID=$!
sleep 1

echo ""
echo "✅  Dashboard is LIVE"
echo "   Local   → http://localhost:$PORT"
echo "   LAN     → http://$LOCAL_IP:$PORT"
echo ""

# Check if ngrok is installed
if command -v ngrok &>/dev/null; then
  echo "🌍  Starting ngrok tunnel for external access..."
  ngrok http $PORT &
  sleep 2
  NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import json,sys; t=json.load(sys.stdin); print(t['tunnels'][0]['public_url'])" 2>/dev/null)
  if [ -n "$NGROK_URL" ]; then
    echo "   Public  → $NGROK_URL"
    echo ""
    echo "📱  Share this URL with anyone: $NGROK_URL"
  else
    echo "   (ngrok tunnel starting — check http://localhost:4040)"
  fi
else
  echo "💡  To share externally, install ngrok:"
  echo "    brew install ngrok"
  echo "    Then run: ngrok http $PORT"
fi

echo ""
echo "   Press Ctrl+C to stop"
echo ""
wait $SERVER_PID
