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
import ui

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
NGROK_AUTHTOKEN = config.get("ngrok_authtoken", "")
NGROK_DOMAIN = config.get("ngrok_domain", "")
PROXY_SECRET = config.get("proxy_secret", "")
RATE_LIMIT_PER_MIN = int(config.get("rate_limit_per_min", 0))

ssh_process = None
public_url = None
startup_spinner = None

ui.enable_ansi_windows()
ui.banner()

if not API_KEY:
    ui.log_line("API Key non configurata in config.json. Modifica il file prima di iniziare.", "ERR")

if not PROXY_SECRET:
    ui.log_line("proxy_secret non configurato. Il proxy sarà accessibile da chiunque conosca l'URL pubblico.", "WARN")
    ui.log_line("Genera una chiave e inseriscila in config.json per proteggere l'accesso.", "WARN")
else:
    ui.box("SICUREZZA - LEGGERE", [
        f"Proxy protetto da secret ({len(PROXY_SECRET)} car.).",
        "",
        "In Cursor, incolla questa secret nel campo 'API Key':",
        PROXY_SECRET,
        "(NON usare 'dummy' o 'sk-dummy' — la richiesta verrebbe rifiutata.)",
    ], ui.RED)

# --- Rate limiting (token bucket per IP) ---
import collections
_rate_buckets = collections.defaultdict(lambda: {"tokens": 0.0, "last": 0.0})
_rate_lock = threading.Lock()

def rate_limit_check(client_ip):
    """True se la richiesta è ammessa, False se sfora il limite."""
    if RATE_LIMIT_PER_MIN <= 0:
        return True
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[client_ip]
        if bucket["last"] == 0.0:
            bucket["tokens"] = float(RATE_LIMIT_PER_MIN)
            bucket["last"] = now
        # Riempi il bucket proporzionalmente al tempo trascorso
        elapsed = now - bucket["last"]
        bucket["tokens"] = min(float(RATE_LIMIT_PER_MIN), bucket["tokens"] + elapsed * (RATE_LIMIT_PER_MIN / 60.0))
        bucket["last"] = now
        if bucket["tokens"] < 1.0:
            return False
        bucket["tokens"] -= 1.0
        return True

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
        print("[Tunnel] Nessuna chiave SSH trovata. Generazione di una chiave SSH locale (necessaria per Pinggy)...")
        try:
            os.makedirs(ssh_dir, exist_ok=True)
            key_path = os.path.join(ssh_dir, "id_ed25519")
            cmd = ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            print("[Tunnel] Chiave SSH generata con successo!")
        except Exception as e:
            print(f"[Tunnel] Errore durante la generazione della chiave SSH: {e}")

def ensure_cloudflared():
    # Verifica se cloudflared è nel PATH del sistema
    try:
        cmd_check = "where" if os.name == "nt" else "which"
        res = subprocess.run([cmd_check, "cloudflared"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            return "cloudflared"
    except Exception:
        pass

    # Verifica se è presente nella cartella del progetto
    bin_name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    bin_path = os.path.join(SCRIPT_DIR, bin_name)
    if os.path.exists(bin_path):
        return bin_path

    # Scarica automaticamente su Windows se mancante
    if os.name == "nt":
        print("[Tunnel] Cloudflare Tunnel (cloudflared.exe) non trovato nella cartella del progetto.")
        print("[Tunnel] Download in corso del binario ufficiale di Cloudflare (nessun limite di tempo)...")
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as response, open(bin_path, "wb") as out_file:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                block_size = 1024 * 1024 # 1MB
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded += len(buffer)
                    out_file.write(buffer)
                    if total_size:
                        percent = int((downloaded / total_size) * 100)
                        sys.stdout.write(f"\r[Tunnel] Download: {percent}% ({downloaded // (1024*1024)}MB / {total_size // (1024*1024)}MB)")
                        sys.stdout.flush()
                print("\n[Tunnel] Download completato con successo!")
            return bin_path
        except Exception as e:
            print(f"\n[Tunnel] Errore nel download di cloudflared: {e}")
            return None
    else:
        print("[Tunnel] Cloudflare Tunnel non trovato. Su macOS/Linux installalo tramite package manager (es. 'brew install cloudflared').")
        return None

def ensure_ngrok():
    try:
        cmd_check = "where" if os.name == "nt" else "which"
        res = subprocess.run([cmd_check, "ngrok"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            return "ngrok"
    except Exception:
        pass

    bin_name = "ngrok.exe" if os.name == "nt" else "ngrok"
    bin_path = os.path.join(SCRIPT_DIR, bin_name)
    if os.path.exists(bin_path):
        return bin_path

    print("[Tunnel] ngrok non trovato nel PATH né nella cartella del progetto.")
    print("[Tunnel] Download in corso di ngrok...")

    if os.name == "nt":
        url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
        zip_path = os.path.join(SCRIPT_DIR, "ngrok.zip")
    elif sys.platform == "darwin":
        url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-amd64.zip"
        zip_path = os.path.join(SCRIPT_DIR, "ngrok.zip")
    else:
        url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip"
        zip_path = os.path.join(SCRIPT_DIR, "ngrok.zip")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as response, open(zip_path, "wb") as out_file:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            block_size = 1024 * 1024
            while True:
                buffer = response.read(block_size)
                if not buffer:
                    break
                downloaded += len(buffer)
                out_file.write(buffer)
                if total_size:
                    percent = int((downloaded / total_size) * 100)
                    sys.stdout.write(f"\r[Tunnel] Download: {percent}%")
                    sys.stdout.flush()
        print("\n[Tunnel] Download completato. Estrazione...")

        import zipfile
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(SCRIPT_DIR)
        try:
            os.remove(zip_path)
        except Exception:
            pass
        print("[Tunnel] ngrok estratto con successo!")
        return bin_path
    except Exception as e:
        print(f"[Tunnel] Errore nel download di ngrok: {e}")
        return None

def start_tunnel():
    global ssh_process, public_url, startup_spinner
    
    provider = config.get("tunnel_provider", "ngrok").lower()
    startup_spinner = ui.Spinner(f"Avvio tunnel ({provider})...")
    startup_spinner.start()
    
    if provider == "ngrok":
        if not NGROK_AUTHTOKEN:
            print("[Tunnel] ⚠️ ngrok_authtoken non configurato in config.json. Ripiego su Cloudflare quick tunnel...")
            provider = "cloudflare"
        else:
            ngrok_path = ensure_ngrok()
            if not ngrok_path:
                print("[Tunnel] ngrok non disponibile. Ripiego su Cloudflare quick tunnel...")
                provider = "cloudflare"
            else:
                try:
                    subprocess.run([ngrok_path, "config", "add-authtoken", NGROK_AUTHTOKEN],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                except Exception as e:
                    print(f"[Tunnel] Errore configurazione authtoken ngrok: {e}")
                    provider = "cloudflare"
                else:
                    cmd = [ngrok_path, "http", str(PORT), "--log=stdout"]
                    if NGROK_DOMAIN:
                        cmd.extend(["--domain", NGROK_DOMAIN])
                        print(f"[Tunnel] Avvio ngrok con dominio statico: {NGROK_DOMAIN} (porta {PORT})...")
                    else:
                        print(f"[Tunnel] ⚠️ ngrok_domain non configurato. Verrà usato un URL temporaneo.")
                        print(f"[Tunnel] Per un dominio stabile, aggiungi 'ngrok_domain' in config.json (es. your-name.ngrok.app)")
                        print(f"[Tunnel] Avvio ngrok (porta {PORT})...")
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
                        print(f"[Tunnel] Errore nell'avvio di ngrok: {e}")
                        print("[Tunnel] Ripiego su Cloudflare quick tunnel...")
                        provider = "cloudflare"

    if provider == "cloudflare":
        cf_path = ensure_cloudflared()
        if not cf_path:
            print("[Tunnel] Cloudflare non disponibile. Ripiego su Pinggy (limite 60 min)...")
            provider = "pinggy"
        else:
            print(f"[Tunnel] Avvio del tunnel Cloudflare (porta {PORT}) in corso...")
            cmd = [cf_path, "tunnel", "--url", f"http://localhost:{PORT}"]
            try:
                ssh_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, # cloudflared scrive i log su stderr
                    stdin=subprocess.PIPE,
                    text=True,
                    bufsize=1
                )
            except Exception as e:
                print(f"[Tunnel] Errore nell'avvio di cloudflared: {e}")
                print("[Tunnel] Ripiego su Pinggy...")
                provider = "pinggy"

    if provider == "pinggy":
        ensure_ssh_key()
        print(f"[Tunnel] Avvio del tunnel SSH Pinggy (porta {PORT}) in corso...")
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
            print(f"[Tunnel] Errore nell'avvio del tunnel SSH Pinggy: {e}")
            return

    def read_output():
        global public_url
        if provider == "cloudflare":
            url_regex = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")
        elif provider == "ngrok":
            url_regex = re.compile(r"https://[a-zA-Z0-9.-]+\.ngrok(?:-free)?\.app")
        else:
            url_regex = re.compile(r"https?://[a-zA-Z0-9.-]+\.pinggy(?:-free)?\.link")
            
        while ssh_process.poll() is None:
            line = ssh_process.stdout.readline()
            if not line:
                break
            clean_line = line.strip()
            
            # Cerca il link pubblico generato
            match = url_regex.search(line)
            if match and not public_url:
                found_url = match.group(0)
                if provider == "pinggy" and found_url.startswith("http://"):
                    public_url = "https://" + found_url[7:]
                else:
                    public_url = found_url
                
                if startup_spinner:
                    startup_spinner.stop()
                ui.box("CURSOR PROXY ATTIVO", [
                    f"Tunnel:  {provider}",
                    f"URL:     {public_url}/v1",
                    "",
                    "In Cursor > Settings > Models > OpenAI API:",
                    "  - Override OpenAI Base URL: incolla l'URL sopra",
                    "  - API Key: incolla la tua proxy_secret",
                    "  - Add model: es. nry-claude-sonnet-4.6",
                ], ui.GREEN)
                log_to_file(f"[Tunnel] Tunnel attivo ({provider}): {public_url}")
            
            # Mostra i log finché non trova il link (filtra il rumore per Cloudflare)
            # Non stampare a console mentre lo spinner è attivo (evita race visiva)
            if not public_url and clean_line and not (startup_spinner and startup_spinner._active):
                if provider == "cloudflare":
                    if "trycloudflare.com" in clean_line or "quick tunnel" in clean_line:
                        ui.log_line(f"[Tunnel] {clean_line}", "INFO")
                else:
                    ui.log_line(f"[Tunnel] {clean_line}", "INFO")

    t = threading.Thread(target=read_output, daemon=True)
    t.start()

def cleanup():
    global ssh_process
    if ssh_process:
        print("[Tunnel] Chiusura tunnel...")
        ssh_process.terminate()
        try:
            ssh_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ssh_process.kill()
        print("[Tunnel] Tunnel spento.")

class BynaraProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        message = "%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format%args)
        log_to_file(message)

    def _send_json_error(self, status, public_message, log_detail=None):
        """Invia un errore JSON generico al client; dettagli solo nel log."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": {"message": public_message}}).encode('utf-8'))
        if log_detail:
            log_to_file(log_detail)

    def _check_access(self):
        """Verifica secret e rate limit. Restituisce True se la richiesta è ammessa."""
        client_ip = self.client_address[0]

        # 1) Rate limit
        if not rate_limit_check(client_ip):
            log_to_file(f"[Auth] Rate limit superato per {client_ip}")
            self._send_json_error(429, "Too many requests", f"[Auth] 429 Rate limit per {client_ip}")
            return False

        # 2) Secret (se configurato)
        if PROXY_SECRET:
            auth = self.headers.get("Authorization", "")
            token = ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            if token != PROXY_SECRET:
                log_to_file(f"[Auth] Secret non valido da {client_ip}")
                self._send_json_error(401, "Unauthorized", f"[Auth] 401 Secret non valido da {client_ip}")
                return False
        return True

    def do_OPTIONS(self):
        log_to_file(f"[OPTIONS] Richiesta da {self.address_string()} per {self.path}")
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        log_to_file(f"[GET] Richiesta da {self.address_string()} per {self.path}")
        if not self._check_access():
            return
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
                self._send_json_error(500, "Proxy error", f"[GET] 500 Errore: {e}")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            log_to_file(f"[GET] 404 Not Found per path: {self.path}")

    def do_POST(self):
        log_to_file(f"[POST] Richiesta da {self.address_string()} per {self.path}")
        if not self._check_access():
            return
        if self.path in ("/chat/completions", "/v1/chat/completions", "/v1/chat/completions/"):
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                body = json.loads(post_data.decode('utf-8'))
            except Exception as e:
                self._send_json_error(400, "Invalid JSON", f"[POST] 400 JSON invalido: {e}")
                return

            original_model = body.get("model", "")
            # Rimuove il prefisso prima di inoltrare la richiesta
            translated_model = original_model
            if MODEL_PREFIX and original_model.startswith(MODEL_PREFIX):
                translated_model = original_model[len(MODEL_PREFIX):]
            
            body["model"] = translated_model
            log_to_file(f"[Proxy] Inoltro richiesta: {original_model} -> {translated_model}")

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

            req_start = time.time()
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
                    duration = time.time() - req_start
                    ui.log_request("POST", self.path, original_model, translated_model, response.status, duration)
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
                duration = time.time() - req_start
                ui.log_request("POST", self.path, original_model, translated_model, e.code, duration)
                log_to_file(f"[POST] HTTPError {e.code}: {err_data.decode('utf-8', errors='ignore')}")
            except Exception as e:
                duration = time.time() - req_start
                ui.log_request("POST", self.path, original_model, translated_model, 500, duration)
                self._send_json_error(500, "Proxy error", f"[POST] Errore Generico: {e}")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            log_to_file(f"[POST] 404 Not Found per path: {self.path}")

def run():
    provider = config.get("tunnel_provider", "ngrok").lower()
    auth_status = "Abilitata" if PROXY_SECRET else "DISABILITATA"
    ui.box("Configurazione", [
        f"Porta:           {PORT}",
        f"Tunnel:          {provider}",
        f"Provider:        {TARGET_BASE_URL}",
        f"Prefisso modelli: {MODEL_PREFIX or '(nessuno)'}",
        f"Autenticazione:  {auth_status}",
        f"Rate limit:      {RATE_LIMIT_PER_MIN if RATE_LIMIT_PER_MIN > 0 else 'illimitato'} req/min",
    ], ui.CYAN)
    start_tunnel()
    server_address = ('', PORT)
    httpd = http.server.HTTPServer(server_address, BynaraProxyHandler)
    ui.log_line(f"Server proxy locale avviato su http://localhost:{PORT}", "OK")
    ui.log_line("Premi Ctrl+C per fermare il programma.", "INFO")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if startup_spinner:
            startup_spinner.stop()
        ui.log_line("Spegnimento server...", "INFO")
        httpd.server_close()
        cleanup()

if __name__ == '__main__':
    run()
