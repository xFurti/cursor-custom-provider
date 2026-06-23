#!/bin/bash
echo "Avvio del proxy locale e del tunnel..."
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
python3 "$SCRIPT_DIR/cursor_proxy.py"
