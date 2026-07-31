#!/bin/bash
# MallPlus Activation Dashboard — Startup Script
cd "$(dirname "$0")"

PORT=8081
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "192.168.1.71")

echo ""
echo "🚀 MallPlus Alpha 1 Activation Dashboard"
echo "════════════════════════════════════════"

# Kill any existing instance on port 8081
lsof -ti:$PORT | xargs kill -9 2>/dev/null && echo "♻️  Stopped previous instance on :$PORT"

echo "🚀  Starting server on port $PORT..."
python3 server.py &
SERVER_PID=$!
sleep 1

echo ""
echo "✅  Dashboard is LIVE"
echo "   Local  → http://localhost:$PORT"
echo "   LAN    → http://$LOCAL_IP:$PORT"
echo ""

# Start ngrok tunnel for port 8081
if command -v ngrok &>/dev/null; then
  echo "🌍  Starting ngrok tunnel..."
  # Kill existing ngrok on port 8081 if any
  pkill -f "ngrok.*8081" 2>/dev/null
  ngrok http $PORT --log=stdout &
  sleep 3
  NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "
import json,sys
try:
  d=json.load(sys.stdin)
  tunnels=d.get('tunnels',[])
  for t in tunnels:
    if '8081' in t.get('config',{}).get('addr',''):
      print(t['public_url'])
      break
  else:
    # fallback: print first tunnel
    if tunnels: print(tunnels[0]['public_url'])
except: pass
" 2>/dev/null)
  if [ -n "$NGROK_URL" ]; then
    echo "   Public → $NGROK_URL"
    echo ""
    echo "📱  Share this URL: $NGROK_URL"
  else
    echo "   (check http://localhost:4040 for ngrok URL)"
  fi
else
  echo "💡  To share externally: ngrok http $PORT"
fi

echo ""
echo "   Press Ctrl+C to stop"
echo ""
wait $SERVER_PID
