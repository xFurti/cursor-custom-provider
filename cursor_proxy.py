import http.server
import urllib.request
import urllib.error
import json
import sys
import os
import subprocess
import threading
import time
import re

# Determina i percorsi relativi allo script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "proxy_log.txt")

# Carica la configurazione
config = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[Errore] Impossibile caricare config.json: {e}")
else:
    print("[Info] config.json non trovato. Saranno usati i parametri predefiniti di Bynara.")

PORT = config.get("port", 8080)
API_KEY = config.get("api_key", "")
TARGET_BASE_URL = config.get("target_base_url", "https://router.bynara.id/v1")
MODEL_PREFIX = config.get("model_prefix", "nry-")

ssh_process = None
public_url = None

if not API_KEY:
    print("\n[⚠️ ATTENZIONE] API Key non configurata in config.json!")
    print("Modifica il file config.json inserendo la tua chiave API prima di iniziare.\n")

def log_to_file(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass

def ensure_ssh_key():
    home = os.path.expanduser("~")
    ssh_dir = os.path.join(home, ".ssh")
    key_files = ["id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"]
    key_exists = False
    
    if os.path.exists(ssh_dir):
        try:
            for f in os.listdir(ssh_dir):
                if f in key_files:
                    key_exists = True
                    break
        except Exception:
            pass
            
    if not key_exists:
        print("[Tunnel] Nessuna chiave SSH trovata. Generazione di una chiave SSH locale (necessaria per il tunnel)...")
        try:
            os.makedirs(ssh_dir, exist_ok=True)
            key_path = os.path.join(ssh_dir, "id_ed25519")
            cmd = ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            print("[Tunnel] Chiave SSH generata con successo!")
        except Exception as e:
            print(f"[Tunnel] Errore durante la generazione della chiave SSH: {e}")

def start_tunnel():
    global ssh_process, public_url
    ensure_ssh_key()
    print(f"[Tunnel] Avvio del tunnel SSH (porta {PORT}) in corso...")
    
    # Comando SSH per Pinggy
    cmd = [
        "ssh", 
        "-p", "443", 
        "-o", "StrictHostKeyChecking=no", 
        "-o", "ServerAliveInterval=30",
        f"-R0:localhost:{PORT}", 
        "free@a.pinggy.io"
    ]
    
    try:
        ssh_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1
        )
    except Exception as e:
        print(f"[Tunnel] Errore nell'avvio del tunnel SSH: {e}")
        print("[Tunnel] Assicurati che l'OpenSSH Client sia abilitato sul tuo sistema (standard su Win 10/11, macOS e Linux).")
        return

    def read_output():
        global public_url
        url_regex = re.compile(r"https://[a-zA-Z0-9.-]+\.pinggy(?:-free)?\.link")
        while ssh_process.poll() is None:
            line = ssh_process.stdout.readline()
            if not line:
                break
            clean_line = line.strip()
            
            # Cerca il link HTTPS pubblico generato da Pinggy
            match = url_regex.search(line)
            if match and not public_url:
                public_url = match.group(0)
                print("\n" + "="*70)
                print("💥 CURSOR PROXY ATTIVO ED ESPOSTO! 💥")
                print("👉 Configura Cursor inserendo questo URL (Override OpenAI Base URL):")
                print(f"   {public_url}/v1")
                print("="*70 + "\n")
                log_to_file(f"[Tunnel] Tunnel attivo: {public_url}")
            
            # Se non ha ancora trovato l'URL, mostra i log per diagnostica
            if not public_url and clean_line:
                print(f"[Tunnel LOG] {clean_line}")

    t = threading.Thread(target=read_output, daemon=True)
    t.start()

def cleanup():
    global ssh_process
    if ssh_process:
        print("[Tunnel] Chiusura tunnel SSH...")
        ssh_process.terminate()
        try:
            ssh_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ssh_process.kill()
        print("[Tunnel] Tunnel SSH spento.")

class BynaraProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        message = "%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format%args)
        log_to_file(message)

    def do_OPTIONS(self):
        log_to_file(f"[OPTIONS] Richiesta da {self.address_string()} per {self.path}")
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        log_to_file(f"[GET] Richiesta da {self.address_string()} per {self.path}")
        if self.path in ("/models", "/v1/models", "/v1/models/"):
            url = f"{TARGET_BASE_URL}/models"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json"
                }
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as response:
                    data = json.loads(response.read().decode())
                    # Aggiunge il prefisso configurato a tutti i modelli
                    if "data" in data and isinstance(data["data"], list):
                        for item in data["data"]:
                            if "id" in item and MODEL_PREFIX:
                                item["id"] = f"{MODEL_PREFIX}{item['id']}"
                    
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode('utf-8'))
                    log_to_file("[GET] 200 Success")
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
                log_to_file(f"[GET] 500 Errore: {e}")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            log_to_file(f"[GET] 404 Not Found per path: {self.path}")

    def do_POST(self):
        log_to_file(f"[POST] Richiesta da {self.address_string()} per {self.path}")
        if self.path in ("/chat/completions", "/v1/chat/completions", "/v1/chat/completions/"):
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                body = json.loads(post_data.decode('utf-8'))
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"Invalid JSON: {e}".encode())
                log_to_file(f"[POST] 400 JSON invalido: {e}")
                return

            original_model = body.get("model", "")
            # Rimuove il prefisso prima di inoltrare la richiesta
            translated_model = original_model
            if MODEL_PREFIX and original_model.startswith(MODEL_PREFIX):
                translated_model = original_model[len(MODEL_PREFIX):]
            
            body["model"] = translated_model
            msg = f"[Proxy] Inoltro richiesta: {original_model} -> {translated_model}"
            print(msg)
            log_to_file(msg)

            req_data = json.dumps(body).encode('utf-8')
            req = urllib.request.Request(
                f"{TARGET_BASE_URL}/chat/completions",
                data=req_data,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    self.send_response(response.status)
                    for header, value in response.getheaders():
                        if header.lower() in ("content-length", "transfer-encoding"):
                            continue
                        self.send_header(header, value)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    while True:
                        chunk = response.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    log_to_file(f"[POST] Successo streaming per {translated_model}")
            except urllib.error.HTTPError as e:
                err_data = e.read()
                self.send_response(e.code)
                for header, value in e.getheaders():
                    if header.lower() in ("content-length", "transfer-encoding"):
                        continue
                    self.send_header(header, value)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_data)
                log_to_file(f"[POST] HTTPError {e.code}: {err_data.decode('utf-8', errors='ignore')}")
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                err_resp = {"error": {"message": f"Errore Proxy: {str(e)}"}}
                self.wfile.write(json.dumps(err_resp).encode())
                log_to_file(f"[POST] Errore Generico: {e}")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            log_to_file(f"[POST] 404 Not Found per path: {self.path}")

def run():
    start_tunnel()
    server_address = ('', PORT)
    httpd = http.server.HTTPServer(server_address, BynaraProxyHandler)
    print(f"[Proxy] Server proxy locale avviato su http://localhost:{PORT}")
    print("Premi Ctrl+C per fermare il programma.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[Proxy] Spegnimento server...")
        httpd.server_close()
        cleanup()

if __name__ == '__main__':
    run()
