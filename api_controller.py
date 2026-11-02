import webview
import os
import json
import sys
import requests
import ctypes
import pyaudio
import numpy as np
import threading
import time
import socketio
import serial
import serial.tools.list_ports

from roip_client import RoIPClientManager
from database_manager import DatabaseManager

CONFIG_FILE = "config.json"
SETTINGS_FILE = "settings.json"

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long)
    ]

class RoIPLoginAPI:
    """
    Kelas pengendali utama yang bertindak sebagai jembatan (API) antara Python dan frontend JavaScript.
    Menangani autentikasi, komunikasi REST API server, serta manajemen RoIP client.
    """
    def __init__(self):
        self.window = None
        self.is_maximized = False
        self.current_user = None   # Menyimpan objek data user dari server (id, username, full_name, role, dll)
        self.auth_token = None     # Menyimpan API_SECRET_TOKEN yang dikembalikan server saat login

        self.pyaudio_instance = pyaudio.PyAudio()
        self.is_recording = False
        self.audio_frames = []
        self.recording_thread = None
        self.is_testing_mic = False
        
        # Inisialisasi Thread Lock untuk keamanan akses memori audio secara multi-thread
        self.audio_lock = threading.Lock()
        
        # Konfigurasi Audio Stream
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.RATE = 16000  # Standar radio/komunikasi (16kHz)
        self.CHUNK = 1024
        
        # Memuat konfigurasi server dari file config.json secara otomatis saat diinisialisasi
        config = self.load_config()
        self.ip_server = config.get('ip_server', '10.18.200.21')
        self.port = config.get('port', '3002')
        self.api_base_url = f"http://{self.ip_server}:{self.port}/api"
        
        # Inisialisasi Socket.io Client untuk Python
        self.sio = socketio.Client()
        self.setup_socket_events()

        # Inisialisasi manajer status RoIP & Database lokal cache
        self.roip_manager = RoIPClientManager()
        self.db_manager = DatabaseManager()
        
        # Status room aktif saat ini
        self.current_room_id = 1

    def setup_socket_events(self):
        @self.sio.event
        def connect():
            print("[SOCKET.IO] Terhubung ke WebSocket server pusat.")

        @self.sio.event
        def disconnect():
            print("[SOCKET.IO] Terputus dari WebSocket server pusat.")

        @self.sio.on('floor_granted')
        def on_floor_granted(data):
            print("[PTT] Floor diizinkan oleh server:", data)

        @self.sio.on('floor_denied')
        def on_floor_denied(data):
            print("[PTT] Floor ditolak oleh server:", data)

        # [TAMBAHAN] Meneruskan event server ke frontend agar UI selalu sinkron secara real-time
        @self.sio.on('update_user_connection')
        def on_update_user_connection(data):
            print("[SOCKET.IO] Update koneksi user diterima:", data)
            if self.window:
                try:
                    # Picu fungsi JavaScript di index.html secara otomatis
                    self.window.evaluate_js(f"if(typeof loadOnlineMembers === 'function') {{ loadOnlineMembers(); }}")
                except Exception as e:
                    print("Gagal mengevaluasi JS update_user_connection:", e)

        @self.sio.on('user_room_changed')
        def on_user_room_changed(data):
            print("[SOCKET.IO] User pindah room diterima:", data)
            if self.window:
                try:
                    self.window.evaluate_js(f"if(typeof loadOnlineMembers === 'function') {{ loadOnlineMembers(); }}")
                except Exception as e:
                    print("Gagal mengevaluasi JS user_room_changed:", e)

    def connect_socket(self):
        if not self.sio.connected:
            try:
                server_url = f"http://{self.ip_server}:{self.port}"
                # Sertakan auth token jika tersedia
                auth_data = {"token": self.auth_token} if self.auth_token else {}
                self.sio.connect(server_url, auth=auth_data)
            except Exception as e:
                print(f"[SOCKET.IO ERROR] Gagal terhubung ke {server_url}: {e}")

    def set_window(self, window):
            self.window = window
    
    def close_window(self):
        try:
            if self.sio.connected:
                self.sio.disconnect()
            if self.window:
                self.window.destroy()
        except Exception:
            pass
        sys.exit(0)

    def minimize_window(self):
        if self.window:
            try:
                self.window.minimize()
            except Exception as e:
                print("Gagal minimize:", e)

    def maximize_window(self):
        if self.window:
            try:
                screens = webview.screens
                if screens:
                    screen_width = screens[0].width
                    screen_height = screens[0].height
                    
                    if self.is_maximized:
                        self.window.resize(1280, 800)
                        pos_x = int((screen_width - 1280) / 2)
                        pos_y = int((screen_height - 800) / 2)
                        self.window.move(pos_x, pos_y)
                        self.is_maximized = False
                    else:
                        rect = RECT()
                        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
                        
                        work_width = rect.right - rect.left
                        work_height = rect.bottom - rect.top
                        
                        self.window.move(rect.left, rect.top)
                        self.window.resize(work_width, work_height)
                        self.is_maximized = True
            except Exception as e:
                print("Gagal maximize/restore:", e)

    def apply_window_state(self):
        if self.window and self.is_maximized:
            try:
                rect = RECT()
                ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
                self.window.move(rect.left, rect.top)
                self.window.resize(rect.right - rect.left, rect.bottom - rect.top)
            except Exception as e:
                print("Gagal apply window state:", e)

    def reset_window_size(self):
        """Mengembalikan ukuran window ke default 1280x800 saat di-drag dari mode maximize"""
        if self.window:
            try:
                # Ubah ukuran kembali ke default
                self.window.resize(1280, 800)
                self.is_maximized = False
                print("[WINDOW] Ukuran direset ke default 1280x800")
            except Exception as e:
                print(f"[WINDOW ERROR] Gagal mereset ukuran: {e}")

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def get_config_json(self):
        config = self.load_config()
        return {
            "success": True,
            "ip_server": config.get("ip_server", "10.18.200.21"),
            "port": config.get("port", "3002"),
            "username": config.get("username", "")
        }

    def save_config(self, ip, port, username):
        config = {'ip_server': ip, 'port': port, 'username': username}
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=4)
        except Exception as e:
            print("Gagal menyimpan config.json:", e)

    def save_settings_to_json(self, settings_data):
        try:
            existing_data = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    try:
                        existing_data = json.load(f)
                    except Exception:
                        pass
            
            if isinstance(settings_data, dict):
                existing_data.update(settings_data)
            else:
                existing_data = settings_data

            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=4)
                
            return {"status": "success", "message": "Settings saved successfully"}
        except Exception as e:
            print("Gagal menyimpan settings.json:", e)
            return {"status": "error", "message": str(e)}

    def load_settings_from_json(self):
        default_settings = {
            "inputDevice": "System default",
            "outputDevice": "System default",
            "micVolume": "100",
            "speakerVolume": "100",
            "activationMode": "Voice Activation",
            "hardwareInterface": {
                "enableHardwarePTT": False,
                "serialPort": "",
                "pttPin": "RTS",
                "baudRate": "9600"
            },
            "shortcuts": {
                "pushToTalk": "Ctrl+",
                "toggleMute": "Ctrl+Shift+M",
                "toggleDeafen": "Ctrl+Shift+D"
            },
            "notifications": {
                "sound": False,
                "native": True,
                "welcomeMessage": "Show once"
            }
        }

        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    saved_data = json.load(f)
                    if isinstance(saved_data, dict):
                        default_settings.update(saved_data)
                    return default_settings
            except Exception as e:
                print("Gagal membaca settings.json:", e)
                
        return default_settings

    def login_action(self, ip, port, username, password):
        self.save_config(ip, port, username)
        self.ip_server = ip
        self.port = port
        self.api_base_url = f"http://{ip}:{port}/api"
        
        try:
            response = requests.post(
                f"{self.api_base_url}/login", 
                json={"username": username, "password": password}, 
                timeout=5
            )
            res_data = response.json()

            if response.status_code == 200 and res_data.get("success"):
                self.auth_token = res_data.get("token")
                self.current_user = res_data.get("data")
                
                # Hubungkan WebSocket Socket.io setelah login sukses
                self.connect_socket()

                print(f"[SUKSES] Login berhasil untuk user: {self.current_user.get('full_name')}")
                
                return {
                    "success": True, 
                    "user": self.current_user
                }
            else:
                return {"success": False, "message": res_data.get("error", "Gagal autentikasi.")}
        except Exception as e:
            return {"success": False, "message": str(e)}

    async def logout(self):
        try:
            self.is_recording = False
            
            if self.sio.connected:
                self.sio.disconnect()
                
            self.current_user = None
            self.auth_token = None

            if self.window:
                self.window.destroy()

            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_session_data(self):
        if not self.current_user:
            return {"success": False, "message": "Belum ada sesi aktif"}
        
        return {
            "success": True,
            "user": self.current_user,
            "token": self.auth_token
        }

    def _get_device_index(self, device_name, is_output=False):
        """Mencari index device yang kompatibel dengan sounddevice/PyAudio"""
        if not device_name or device_name == "System default":
            return None
        
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            
            # Cari berdasarkan kecocokan nama perangkat
            for dev in devices:
                if device_name.strip().lower() in dev['name'].strip().lower():
                    if is_output and dev['max_output_channels'] > 0:
                        return dev['index']
                    elif not is_output and dev['max_input_channels'] > 0:
                        return dev['index']
        except Exception as e:
            print(f"[ERROR] Gagal mencari device index: {e}")
            
        return None

    def test_speaker(self, output_device_name=None):
        """Fungsi yang dipanggil dari JavaScript untuk test speaker"""
        try:
            duration = 1.0  # 1 detik
            frequency = 440.0  # Nada A4
            
            t = np.linspace(0, duration, int(self.RATE * duration), endpoint=False)
            wave_data = (np.sin(2 * np.pi * frequency * t) * 32767).astype(np.int16).tobytes()

            output_index = self._get_device_index(output_device_name, is_output=True)

            stream = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                output=True,
                output_device_index=output_index,
                frames_per_buffer=self.CHUNK
            )

            stream.write(wave_data)
            stream.stop_stream()
            stream.close()
            return {"success": True, "message": "Test speaker berhasil diputar."}
        except Exception as e:
            print(f"[SPEAKER TEST ERROR] {e}")
            return {"success": False, "message": str(e)}

    def start_mic_test_record(self, input_device_name=None):
        try:
            self.stop_audio_test()
            self.is_recording_mic_test = True
            self.test_audio_frames = []
            
            input_index = self._get_device_index(input_device_name, is_output=False)

            def record_worker():
                stream_in = None
                try:
                    stream_in = self.pyaudio_instance.open(
                        format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        input_device_index=input_index,
                        frames_per_buffer=self.CHUNK
                    )
                    
                    chunk_count = 0
                    while getattr(self, 'is_recording_mic_test', False):
                        data = stream_in.read(self.CHUNK, exception_on_overflow=False)
                        with self.audio_lock:
                            self.test_audio_frames.append(data)
                        
                        # --- TAMBAHAN UPDATE MIC METER (DI-THROTTLE SETIAP 5 CHUNK) ---
                        if chunk_count % 3 == 0:
                            try:
                                audio_data = np.frombuffer(data, dtype=np.int16)
                                if len(audio_data) > 0:
                                    rms = np.sqrt(np.mean(audio_data**2))
                                    
                                    # --- PENGAMAN JIKA RMS BERNILAI NAN ATAU INVALID ---
                                    if np.isnan(rms):
                                        level = 0
                                    elif rms <= 20:
                                        level = 1
                                    elif rms <= 30:
                                        level = 2
                                    elif rms <= 50:
                                        level = 3
                                    elif rms <= 60:
                                        level = 4
                                    elif rms <= 80:
                                        level = 5
                                    elif rms <= 100:
                                        level = 6
                                    elif rms <= 130:
                                        level = 7
                                    else:
                                        level = 8
                                    
                                    print(f"[DEBUG RMS] rms: {rms} | level: {level}")

                                    if self.window:
                                        self.window.evaluate_js(f"if(typeof updateMicMeter === 'function') {{ updateMicMeter({level}); }}")
                            except Exception as ex:
                                print(f"[METER EXCEPTION] {ex}")

                        chunk_count += 1
                except Exception as e:
                    print(f"[MIC RECORD ERROR] {e}")
                finally:
                    if stream_in:
                        try:
                            stream_in.stop_stream()
                            stream_in.close()
                        except:
                            pass
                    # Reset meteran ke 0 saat perekaman berhenti
                    if self.window:
                        try:
                            self.window.evaluate_js("if(typeof updateMicMeter === 'function') { updateMicMeter(0); }")
                        except:
                            pass

            self.mic_test_thread = threading.Thread(target=record_worker)
            self.mic_test_thread.daemon = True
            self.mic_test_thread.start()
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def stop_mic_test_and_playback(self, output_device_name=None):
        try:
            self.is_recording_mic_test = False
            if hasattr(self, 'mic_test_thread') and self.mic_test_thread.is_alive():
                self.mic_test_thread.join(timeout=0.2)

            time.sleep(0.3)  # Jeda aman untuk melepas buffer hardware
            
            self.is_playing_mic_test = True
            output_index = self._get_device_index(output_device_name, is_output=True)

            def playback_worker():
                stream_out = None
                try:
                    stream_out = self.pyaudio_instance.open(
                        format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        output=True,
                        output_device_index=output_index,
                        frames_per_buffer=self.CHUNK
                    )
                    
                    with self.audio_lock:
                        frames = list(self.test_audio_frames)

                    for data in frames:
                        if not getattr(self, 'is_playing_mic_test', False):
                            break
                        stream_out.write(data)
                except Exception as e:
                    print(f"[MIC PLAYBACK ERROR] {e}")
                finally:
                    if stream_out:
                        try:
                            stream_out.stop_stream()
                            stream_out.close()
                        except:
                            pass
                    self.is_playing_mic_test = False

            self.mic_playback_thread = threading.Thread(target=playback_worker)
            self.mic_playback_thread.daemon = True
            self.mic_playback_thread.start()

            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def stop_audio_test(self):
        self.is_recording_mic_test = False
        self.is_playing_mic_test = False
        # Pastikan meteran ikut di-reset ke 0 saat stop dipanggil
        if self.window:
            try:
                self.window.evaluate_js("if(typeof updateMicMeter === 'function') { updateMicMeter(0); }")
            except:
                pass
        return {"success": True}

    def go_to_index(self):
        """Berpindah kembali ke halaman utama index.html secara lokal tanpa HTTP server"""
        if self.window:
            base_path = os.path.dirname(os.path.abspath(__file__))
            index_path = os.path.join(base_path, 'frontend', 'index.html')
            file_url = f"file:///{index_path.replace(os.sep, '/')}"
            self.window.load_url(file_url)

    def go_to_login(self):
        """Berpindah kembali ke halaman login.html secara lokal tanpa HTTP server"""
        try:
            self.is_recording = False
            
            if self.sio.connected:
                self.sio.disconnect()
                
            self.current_user = None
            self.auth_token = None

            if self.window:
                base_path = os.path.dirname(os.path.abspath(__file__))
                login_path = os.path.join(base_path, 'frontend', 'login.html')
                file_url = f"file:///{login_path.replace(os.sep, '/')}"
                self.window.load_url(file_url)
                
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # --- 2. MANAJEMEN PERANGKAT AUDIO ---
    def get_audio_devices(self):
        try:
            import sounddevice as sd
            
            sd._lib.Pa_Terminate()
            sd._lib.Pa_Initialize()
            
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            
            input_devices = []
            output_devices = []
            seen_names = set()

            target_api_index = 0
            for idx, api in enumerate(hostapis):
                if 'WASAPI' in api['name']:
                    target_api_index = idx
                    break
                elif 'MME' in api['name']:
                    target_api_index = idx

            for dev in devices:
                if dev['hostapi'] != target_api_index:
                    continue

                name = dev['name']
                
                if not name or not name.strip():
                    continue
                if "Primary Sound" in name or "Mapper" in name or "@System32" in name:
                    continue

                if dev['max_input_channels'] > 0:
                    if name not in seen_names:
                        input_devices.append({"id": dev['index'], "name": name})
                        seen_names.add(name)

                if dev['max_output_channels'] > 0:
                    if not any(d['name'] == name for d in output_devices):
                        output_devices.append({"id": dev['index'], "name": name})

            return {
                "success": True, 
                "inputs": input_devices, 
                "outputs": output_devices
            }
        except Exception as e:
            print(f"Gagal mengambil perangkat audio: {e}")
            return {"success": False, "inputs": [], "outputs": []}

    # --- 3. MANAJEMEN PORT COM & KANAL ---
    def get_available_com_ports(self):
        """Memindai port COM aktif yang terhubung ke komputer Windows."""
        try:
            ports = serial.tools.list_ports.comports()
            port_list = []
            for port in ports:
                port_list.append({
                    "device": port.device,
                    "description": port.description
                })
            return {"success": True, "ports": port_list}
        except Exception as e:
            return {"success": False, "ports": [], "message": str(e)}

    def fetch_online_members(self):
        """Mengambil daftar user yang sedang online beserta room aktif mereka dari server pusat."""
        if not self.auth_token:
            return {"success": False, "message": "Token tidak tersedia atau belum login"}
            
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        try:
            url = f"{self.api_base_url}/users/online"
            response = requests.get(url, headers=headers, timeout=5)
            return response.json()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def fetch_user_rooms(self):
        if not self.current_user:
            return {"success": False, "message": "User belum login"}
            
        user_id = self.current_user.get("id")
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        
        try:
            url = f"{self.api_base_url}/users/{user_id}/rooms"
            response = requests.get(url, headers=headers, timeout=5)
            res_data = response.json()
            
            if res_data.get("success"):
                return {"success": True, "rooms": res_data.get("rooms", [])}
            else:
                return {"success": False, "message": "Gagal memuat kanal dari server"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def switch_room(self, channel_id):
        """Berpindah channel aktif dan kirim event join_channel ke Socket.io."""
        self.current_room_id = channel_id
        
        if self.current_user:
            self.current_user['current_room'] = channel_id

        if self.sio.connected:
            self.sio.emit('join_channel', str(channel_id))
            
        return self.roip_manager.set_active_channel(channel_id)

    def fetch_channel_chats(self, room_id):
        """Mengambil riwayat chat berdasarkan room_id dari server pusat."""
        if not self.auth_token:
            return {"success": False, "message": "Token tidak tersedia"}
            
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        try:
            url = f"{self.api_base_url}/channels/{room_id}/chats"
            response = requests.get(url, headers=headers, timeout=5)
            return response.json()
        except Exception as e:
            return {"success": False, "message": str(e)}

    # --- 4. MANAJEMEN PTT & SOCKET.IO STREAMING ---
    def start_ptt(self):
        """Dipanggil dari frontend saat tombol PTT ditekan."""
        print("[PTT] Transmisi mulai (Streaming via Socket.io)...")
        
        with self.audio_lock:
            self.is_recording = True
            self.ptt_start_time = time.time()

        # [KONTROL PTT HARDWARE VIA COM] Cek apakah toggle diaktifkan di settings.json
        try:
            settings = self.load_settings_from_json()
            hw_interface = settings.get("hardwareInterface", {})
            is_hardware_enabled = hw_interface.get("enableHardwarePTT", False)
            com_port = hw_interface.get("serialPort")
            ptt_pin = hw_interface.get("pttPin", "RTS").upper()
            baud_rate = int(hw_interface.get("baudRate", 9600))

            # Kondisi jika True (Aktif)
            if is_hardware_enabled and com_port and com_port != "Tidak ada COM yang terdeteksi":
                self.ser_ptt = serial.Serial(com_port, baud_rate, timeout=1)
                
                if ptt_pin == "DTR":
                    self.ser_ptt.setDTR(True)
                else:
                    self.ser_ptt.setRTS(True)
                    
                print(f"[HARDWARE PTT] Port {com_port} AKTIF (True). Pin {ptt_pin} diset HIGH (TRANSMIT).")
            else:
                # Jika toggle True tapi port kosong, berikan peringatan di log tanpa membuat error crash
                if is_hardware_enabled:
                    print("[HARDWARE PTT WARNING] Toggle aktif tetapi Port COM belum dipilih/kosong!")
                else:
                    print("[HARDWARE PTT] Status False / Dinonaktifkan, mengabaikan pemicu COM port.")
        except Exception as e:
            print(f"[HARDWARE PTT ERROR] Gagal mengaktifkan pin PTT: {e}")

        # 1. Kirim request_floor ke server Node.js agar server menginisialisasi WavFileWriter
        if self.sio.connected:
            user_id = self.current_user.get("id") if self.current_user else 1
            username = self.current_user.get("full_name", "Dispatcher")
            self.sio.emit('request_floor', {
                'roomId': str(self.current_room_id),
                'userId': user_id,
                'username': username
            })

        # 2. Jalankan perekaman & streaming audio chunks ke background thread
        self.recording_thread = threading.Thread(target=self._record_and_stream_loop)
        self.recording_thread.daemon = True
        self.recording_thread.start()
        
        return {"success": True}

    def _record_and_stream_loop(self):
        """Looping menangkap stream audio dari mikrofon dan langsung mengirimkannya ke server via Socket.io."""
        try:
            # Ambil setting mic terbaru dari JSON
            settings = self.load_settings_from_json()
            input_device_name = settings.get("inputDevice", "System default")
            input_index = self._get_device_index(input_device_name, is_output=False)

            stream = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                input_device_index=input_index, # <--- Tambahkan index perangkat terpilih disini
                frames_per_buffer=self.CHUNK
            )
            
            while self.is_recording:
                data = stream.read(self.CHUNK, exception_on_overflow=False)
                if data and self.sio.connected:
                    self.sio.emit('audio_stream', data)
                
            stream.stop_stream()
            stream.close()
        except Exception as e:
            print(f"[ERROR PyAudio Stream] Gagal merekam/streaming: {e}")

    def release_ptt(self, room_id=1, user_id=1):
        """Dipanggil dari frontend saat tombol PTT dilepas."""
        print("[PTT] Transmisi berhenti. Menghentikan streaming...")
        self.is_recording = False
        
        if self.recording_thread and self.recording_thread.is_alive():
            self.recording_thread.join(timeout=1.0)

        duration_seconds = int(time.time() - getattr(self, 'ptt_start_time', time.time()))
        duration_seconds = max(1, duration_seconds)

        # [KONTROL PTT HARDWARE VIA COM] Matikan pin saat PTT dilepas
        try:
            if hasattr(self, 'ser_ptt') and self.ser_ptt and self.ser_ptt.is_open:
                settings = self.load_settings_from_json()
                hw_interface = settings.get("hardwareInterface", {})
                ptt_pin = hw_interface.get("pttPin", "RTS").upper()

                # Set pin menjadi False (LOW) kembali
                if ptt_pin == "DTR":
                    self.ser_ptt.setDTR(False)
                else:
                    self.ser_ptt.setRTS(False)

                # Tutup koneksi port serial
                self.ser_ptt.close()
                print("[HARDWARE PTT] Pin PTT dimatikan (False / RECEIVE) & Port ditutup.")
        except Exception as e:
            print(f"[HARDWARE PTT ERROR] Gagal mematikan pin PTT: {e}")

        # Beritahu server via Socket.io bahwa PTT dilepas
        if self.sio.connected:
            r_id = str(room_id or self.current_room_id)
            u_id = user_id or (self.current_user.get("id") if self.current_user else 1)
            
            self.sio.emit('release_floor', {
                'roomId': r_id,
                'userId': u_id,
                'duration': duration_seconds
            })
            print("[PTT] Event release_floor terkirim ke server.")

        return {
            "success": True, 
            "duration": duration_seconds
        }

    # --- 5. MANAJEMEN LOKASI & TRACKING ---
    def update_user_location_realtime(self, latitude, longitude):
        if not self.current_user:
            return {"success": False, "message": "User belum login"}
            
        user_id = self.current_user.get("id")
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        payload = {
            "user_id": user_id,
            "latitude": latitude,
            "longitude": longitude
        }
        
        try:
            url = f"{self.api_base_url}/users/location"
            response = requests.post(url, json=payload, headers=headers, timeout=5)
            return response.json()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def track_user_location_history(self, latitude, longitude):
        if not self.current_user:
            return {"success": False, "message": "User belum login"}
            
        user_id = self.current_user.get("id")
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        payload = {
            "user_id": user_id,
            "latitude": latitude,
            "longitude": longitude
        }
        
        try:
            url = f"{self.api_base_url}/location/track"
            response = requests.post(url, json=payload, headers=headers, timeout=5)
            return response.json()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def fetch_user_location_logs(self, target_user_id=None):
        if not self.current_user:
            return {"success": False, "message": "User belum login"}
            
        userId = target_user_id or self.current_user.get("id")
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        
        try:
            url = f"{self.api_base_url}/location/logs/{userId}"
            response = requests.get(url, headers=headers, timeout=5)
            return response.json()
        except Exception as e:
            return {"success": False, "message": str(e)}

    # --- 6. FITUR DISPATCH & AUDIO KONTROL ---
    def dispatch_merge_channels(self, channel_id_a, channel_id_b):
        print(f"Dispatch diaktifkan: Menggabungkan Channel {channel_id_a} dan {channel_id_b}")
        return {"success": True, "message": f"Channel {channel_id_a} dan {channel_id_b} berhasil digabungkan."}

    def toggle_mute_global(self, mute_state):
        return self.roip_manager.toggle_global_mute(mute_state)

    def toggle_channel_audio(self, channel_id, enabled):
        return self.roip_manager.set_channel_audio_state(channel_id, enabled)