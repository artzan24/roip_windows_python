import webview
import sys
import os
import json
import socket
import threading
import time
import ctypes
from api_controller import RoIPLoginAPI

CONFIG_FILE = "config.json"

# Struktur RECT untuk sistem Windows guna mendeteksi area layar kerja bersih (di atas taskbar)
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long)
    ]

def load_config():
    """Fungsi untuk memuat konfigurasi ip_server dan port dari config.json secara dinamis."""
    default_config = {
        "ip_server": "10.18.200.21",
        "port": "3002"
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                default_config.update(config_data)
        except Exception as e:
            print(f"[WARNING] Gagal membaca config.json, menggunakan default: {e}")
            
    return default_config

def check_tcp_connection(host, port, timeout=3):
    """Fungsi pembantu untuk mengetes koneksi TCP ke host & port tertentu."""
    try:
        socket.setdefaulttimeout(timeout)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False

def check_internet_access(timeout=3):
    """Mengecek koneksi internet publik dengan mencoba menghubungi DNS publik Google (8.8.8.8:53) atau Cloudflare (1.1.1.1:53)."""
    try:
        socket.setdefaulttimeout(timeout)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except Exception:
        return False

def start_connection_monitor(window, ip_server, port):
    """Thread latar belakang untuk memantau koneksi Server Lokal & Internet secara berkala dengan deteksi pemulihan."""
    def monitor_loop():
        time.sleep(5) # Jeda awal saat aplikasi mulai
        
        server_was_down = False
        internet_was_down = False
        
        while True:
            try:
                # Cek apakah objek window masih valid/ada untuk menghindari error evaluate_js saat logout/tutup
                if not window:
                    break

                # 1. Tes Koneksi ke Server Lokal
                server_ok = check_tcp_connection(ip_server, int(port))
                if not server_ok:
                    if not server_was_down:
                        print(f"[MONITOR] Peringatan: Koneksi ke Server Lokal ({ip_server}:{port}) terputus!")
                        try:
                            window.evaluate_js("if(typeof showServerConnectionLostAlert === 'function') { showServerConnectionLostAlert(); }")
                        except Exception:
                            pass
                        server_was_down = True
                else:
                    if server_was_down:
                        print(f"[MONITOR] Info: Koneksi ke Server Lokal ({ip_server}:{port}) kembali terhubung.")
                        try:
                            window.evaluate_js("if(typeof handleConnectionRestored === 'function') { handleConnectionRestored(); }")
                        except Exception:
                            pass
                        server_was_down = False
                
                # 2. Tes Koneksi ke Internet (untuk aset CDN CSS/JS)
                internet_ok = check_internet_access()
                if not internet_ok:
                    if not internet_was_down:
                        print("[MONITOR] Peringatan: Koneksi Internet terputus!")
                        try:
                            window.evaluate_js("if(typeof showInternetConnectionLostAlert === 'function') { showInternetConnectionLostAlert(); }")
                        except Exception:
                            pass
                        internet_was_down = True
                else:
                    if internet_was_down:
                        print("[MONITOR] Info: Koneksi Internet kembali pulih.")
                        internet_was_down = False
                
            except Exception as e:
                # Mengabaikan error socket/koneksi saat aplikasi ditutup
                break
                
            # Cek setiap 10 detik sekali
            time.sleep(10)

    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()

def setup_window_position(window):
    """Mengatur jendela agar berukuran 1280x800 dan terbuka tepat di tengah layar."""
    try:
        screens = webview.screens
        if screens:
            screen_width = screens[0].width
            screen_height = screens[0].height
            width, height = 1280, 800
            
            pos_x = int((screen_width - width) / 2)
            pos_y = int((screen_height - height) / 2)
            
            window.move(pos_x, pos_y)
            window.resize(width, height)
    except Exception as e:
        print("Gagal mengatur posisi awal jendela:", e)

def main():
    # 1. Muat konfigurasi IP Server & Port secara dinamis dari config.json
    config = load_config()
    ip_server = config.get("ip_server", "10.18.200.21")
    port = config.get("port", "3002")
    
    print(f"[CONFIG] Target Server Lokal: {ip_server}:{port}")

    # 2. Inisialisasi objek API Controller
    api = RoIPLoginAPI()

    # 3. Membuat jendela aplikasi berbasis webview (frameless)
    window = webview.create_window(
        'ROIP TIK Polda Bali - Gateway',
        'frontend/login.html',
        js_api=api,
        width=1280,
        height=800,
        frameless=True,
        easy_drag=False,
        resizable=True,
        background_color='#171821'
    )

    api.window = window
    
    # 4. Jalankan background monitor untuk mengecek Server Lokal & Internet
    start_connection_monitor(window, ip_server, port)
    
    # 5. Menjalankan aplikasi pywebview
    webview.start(setup_window_position, window, debug=True)

if __name__ == '__main__':
    main()