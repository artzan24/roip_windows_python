import socket
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
        self.is_recording_mic_test = False
        self.is_playing_mic_test = False
        self.test_audio_frames = []
        self.is_voice_activity_monitoring = False
        self.voice_monitor_thread = None
        self.vad_silence_since = None
        self.vad_last_request_at = 0.0
        self.vad_floor_requested = False
        self.vad_floor_granted = False
        self.vad_hangover_seconds = 2.0
        self._resume_stream_after_mic_test = False

        self.ptt_floor_requested = False
        self.ptt_floor_granted = False
        self.ptt_cancel_pending = False
        self.ptt_request_room_id = None

        self.mic_volume = 100
        self.speaker_volume = 100

        # --- INISIALISASI STATE CONTROL ---
        self.is_mic_muted = False
        self.is_speaker_muted = False
        self.was_transmitting = False
        self.ptt_start_time = 0
        self.is_ptt_active = False

        self.settings_lock = threading.RLock()
        
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
        
        # Muat volume awal dari settings.json jika tersedia
        self._load_volumes_from_settings()

        # Inisialisasi Socket.io Client untuk Python
        self.sio = socketio.Client()
        self.setup_socket_events()

        # Inisialisasi manajer status RoIP & Database lokal cache
        self.roip_manager = RoIPClientManager()
        self.db_manager = DatabaseManager()
        
        # Status room aktif saat ini
        self.current_room_id = 1

        self.start_background_recording_if_needed()

    def _load_volumes_from_settings(self):
        """Memuat pengaturan volume awal dari file settings.json"""
        try:
            settings = self.load_settings_from_json()
            self.mic_volume = int(settings.get("micVolume", 100))
            self.speaker_volume = int(settings.get("speakerVolume", 100))
        except Exception:
            pass

    def setup_socket_events(self):
        @self.sio.event
        def connect():
            print("[SOCKET.IO] Terhubung ke WebSocket server pusat.")
            if self.current_user and self.current_user.get("id"):
                user_id = self.current_user.get("id")
                self.sio.emit('register_user_socket', user_id)
                print(f"[SOCKET.IO] Mengirim event register_user_socket untuk User ID: {user_id}")

        # --- EVENT PTT & STATUS BUSY/READY ---
        @self.sio.on('user_ptt_broadcast')
        def on_user_ptt_broadcast(data):
            print(f"[DEBUG LOG - PTT BROADCAST] Status PTT user lain diterima: {data}")

            if self.window:
                try:
                    safe_data = json.dumps(data)
                    self.window.evaluate_js(
                        f"window.updatePeerPTTStatus && window.updatePeerPTTStatus({safe_data})"
                    )
                except Exception as e:
                    print(f"[ERROR LOG] Gagal mengevaluasi JS user_ptt_broadcast: {e}")

        @self.sio.event
        def disconnect():
            print("[SOCKET.IO] Terputus dari WebSocket server pusat.")

        @self.sio.on('floor_granted')
        def on_floor_granted(data):
            print(f"[PTT FLOOR] Floor diberikan: {data}")

            if self.ptt_cancel_pending:
                print("[PTT FLOOR] Permintaan sudah dibatalkan, melepas floor.")

                self.sio.emit('release_floor', {
                    'roomId': str(self.ptt_request_room_id or self.current_room_id),
                    'userId': str((self.current_user or {}).get('id', ''))
                })

                self.ptt_floor_requested = False
                self.ptt_floor_granted = False
                self.ptt_cancel_pending = False
                self.ptt_request_room_id = None
                return

            self.ptt_floor_requested = False
            self.ptt_floor_granted = True

            current_mode = self.load_settings_from_json().get("activationMode", "Push to Talk")
            if current_mode == "Voice Activation":
                self.vad_floor_requested = False
                self.vad_floor_granted = True
                print("[VAD] Floor diberikan; audio akan dikirim dari stream VAD yang aktif.")
            else:
                # Mekanisme PTT manual tetap seperti semula.
                self.start_ptt()


        @self.sio.on('floor_denied')
        def on_floor_denied(data):
            print(f"[PTT FLOOR] Floor ditolak: {data}")

            self.ptt_floor_requested = False
            self.ptt_floor_granted = False
            self.ptt_cancel_pending = False
            self.ptt_request_room_id = None
            self.vad_floor_requested = False
            self.vad_floor_granted = False
            self.vad_last_request_at = time.time()

            if self.window:
                try:
                    self.window.evaluate_js(
                        "window.handleFloorDenied && window.handleFloorDenied("
                        + json.dumps(data)
                        + ")"
                    )
                except Exception as e:
                    print(f"[PTT FLOOR] Gagal mengirim floor_denied ke frontend: {e}")


        @self.sio.on('floor_released')
        def on_floor_released(data):
            print(f"[PTT FLOOR] Floor dilepaskan: {data}")

            self.ptt_floor_requested = False
            self.ptt_floor_granted = False
            self.ptt_cancel_pending = False
            self.ptt_request_room_id = None
            self.vad_floor_requested = False
            self.vad_floor_granted = False
            self.vad_silence_since = None

            # Beri tahu frontend bahwa channel sudah READY
            if self.window:
                try:
                    safe_data = json.dumps(data)

                    self.window.evaluate_js(
                        f"if(typeof handlePeerPTTStatus === 'function') "
                        f"{{ handlePeerPTTStatus({safe_data}, false); }}"
                    )

                except Exception as e:
                    print(f"[PTT FLOOR] Gagal update status READY: {e}")

        @self.sio.on('update_user_connection')
        def on_update_user_connection(data):
            print(f"[DEBUG LOG - CONNECTION] Update koneksi user diterima: {data}")
            if self.window:
                try:
                    self.window.evaluate_js("if(typeof loadOnlineMembers === 'function') { loadOnlineMembers(); }")
                except Exception as e:
                    print(f"[ERROR LOG] Gagal mengevaluasi JS update_user_connection: {e}")

        @self.sio.on('user_room_changed')
        def on_user_room_changed(data):
            print(f"[DEBUG LOG - ROOM] User pindah room diterima: {data}")
            if self.window:
                try:
                    self.window.evaluate_js("if(typeof loadOnlineMembers === 'function') { loadOnlineMembers(); }")
                except Exception as e:
                    print(f"[ERROR LOG] Gagal mengevaluasi JS user_room_changed: {e}")

   
        @self.sio.on('audio_stream')
        def on_audio_stream(data):
            try:
                # Jika server mengirim buffer biner mentah atau JSON object
                if isinstance(data, bytes):
                    self.handle_incoming_channel_audio(self.current_room_id, data)
                elif isinstance(data, dict):
                    channel_id = data.get('roomId') or self.current_room_id
                    audio_bytes = data.get('audioData')
                    if audio_bytes:
                        self.handle_incoming_channel_audio(channel_id, audio_bytes)
            except Exception as e:
                print(f"[SOCKET AUDIO ERROR] Gagal memproses audio masuk: {e}")

    def connect_socket(self):
        if not self.sio.connected:
            try:
                server_url = f"http://{self.ip_server}:{self.port}"
                auth_data = {"token": self.auth_token} if self.auth_token else {}
                self.sio.connect(server_url, auth=auth_data)
                print(f"[SOCKET.IO] Berhasil menghubungkan Socket.io ke {server_url}")
            except Exception as e:
                print(f"[SOCKET.IO ERROR] Gagal terhubung ke {server_url}: {e}")

    def set_window(self, window):
        self.window = window

    def check_server_status(self):
        try:
            config = self.load_config()
            server_ip = config.get("ip_server", "10.18.200.21")
            server_port = int(config.get("port", 3002))

            socket.create_connection((server_ip, server_port), timeout=2)
            return {
                "success": True, 
                "message": f"Terhubung ke server pusat {server_ip}:{server_port}"
            }
        except OSError:
            try:
                socket.create_connection(("8.8.8.8", 53), timeout=2)
                return {
                    "success": False, 
                    "error": "Internet online, tetapi server pusat tidak dapat dijangkau"
                }
            except OSError:
                return {
                    "success": False, 
                    "error": "Tidak ada koneksi internet / offline"
                }
        except Exception as e:
            return {
                "success": False, 
                "error": str(e)
            }
    
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
        if self.window:
            try:
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

    def save_settings_to_json(self, new_settings):
        import tempfile
        temp_file = None
        with self.settings_lock:
            try:
                if not isinstance(new_settings, dict):
                    return False
                existing_data = self.load_settings_from_json()
                existing_data.update(new_settings)
                for key in ("hardwareInterface", "shortcuts", "notifications"):
                    if isinstance(new_settings.get(key), dict):
                        existing_data[key].update(new_settings[key])
                settings_path = os.path.abspath(SETTINGS_FILE)
                settings_dir = os.path.dirname(settings_path)

                # Buat file sementara dengan nama unik
                fd, temp_file = tempfile.mkstemp(
                    prefix="settings_",
                    suffix=".tmp",
                    dir=settings_dir
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        existing_data,
                        f,
                        indent=4,
                        ensure_ascii=False
                    )
                    f.flush()
                    os.fsync(f.fileno())

                # Retry jika Windows sementara mengunci file
                for attempt in range(6):
                    try:
                        os.replace(temp_file, settings_path)
                        temp_file = None
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.2 * (attempt + 1))

                print("[SETTINGS] Berhasil menyimpan settings.json")
                return True

            except Exception as e:
                print(f"[SETTINGS ERROR] Gagal menyimpan settings.json: {e}")
                return False

            finally:
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass

    def load_settings_from_json(self):
        default_settings = {
            "inputDevice": "System default",
            "outputDevice": "System default",
            "micVolume": "100",
            "speakerVolume": "100",
            "activationMode": "Push to Talk",
            "voiceThreshold": "-40",
            "hardwareInterface": {
                "enableHardwarePTT": False,
                "serialPort": "",
                "pttPin": "RTS",
                "baudRate": "9600"
            },
            "shortcuts": {
                "pushToTalk": "SPACE",
                "toggleMute": "Ctrl+Shift+M",
                "toggleDeafen": "Ctrl+Shift+D"
            },
            "notifications": {
                "sound": False,
                "native": True,
                "welcomeMessage": "Show once"
            }
        }

        # Gunakan lock agar pembacaan tidak bertabrakan dengan proses penyimpanan
        with self.settings_lock:
            try:
                if not os.path.exists(SETTINGS_FILE):
                    return default_settings

                with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
                    content = f.read().strip()

                if not content:
                    return default_settings

                saved_data = json.loads(content)

                if not isinstance(saved_data, dict):
                    print("[SETTINGS ERROR] Format settings.json harus berupa object JSON.")
                    return default_settings

                # Gabungkan pengaturan utama
                default_settings.update(saved_data)

                # Pastikan bagian bertingkat tetap memiliki default
                for key in ("hardwareInterface", "shortcuts", "notifications"):
                    if isinstance(saved_data.get(key), dict):
                        default_settings[key].update(saved_data[key])

                return default_settings

            except json.JSONDecodeError as e:
                print(
                    f"[SETTINGS ERROR] JSON tidak valid: {e}\n"
                    f"[SETTINGS DEBUG] File: {os.path.abspath(SETTINGS_FILE)}"
                )

            except Exception as e:
                print(f"[SETTINGS ERROR] Gagal membaca settings.json: {e}")

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
                
                self.connect_socket()
                self.start_background_recording_if_needed()

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

    def save_audio_volumes(self, mic_vol, speaker_vol):
        try:
            self.mic_volume = int(mic_vol)
            self.speaker_volume = int(speaker_vol)
            settings = self.load_settings_from_json()
            settings["micVolume"] = str(self.mic_volume)
            settings["speakerVolume"] = str(self.speaker_volume)
            self.save_settings_to_json(settings)
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _get_device_index(self, device_name, is_output=False):
        if not device_name or device_name == "System default" or device_name.strip() == "":
            return None
        try:
            device_count = self.pyaudio_instance.get_device_count()
            for i in range(device_count):
                dev_info = self.pyaudio_instance.get_device_info_by_index(i)
                dev_name = dev_info.get('name', '')
                channels = dev_info.get('maxOutputChannels') if is_output else dev_info.get('maxInputChannels')
                if channels > 0:
                    if device_name.strip().lower() in dev_name.strip().lower() or dev_name.strip().lower() in device_name.strip().lower():
                        return i
        except Exception as e:
            print(f"[ERROR] Gagal mencocokkan device index PyAudio: {e}")
        return None

    def test_speaker(self, output_device_name=None):
        try:
            self.is_playing_speaker_test = True
            notes = [523.25, 659.25, 783.99, 1046.50, 783.99, 659.25, 523.25] 
            note_duration = 0.4 
            single_pattern = np.array([], dtype=np.float32)
            for freq in notes:
                t = np.linspace(0, note_duration, int(self.RATE * note_duration), endpoint=False)
                envelope = np.sin(np.pi * t / note_duration) 
                wave = np.sin(2 * np.pi * freq * t) * envelope * 32767
                single_pattern = np.concatenate((single_pattern, wave))
                
            silence = np.zeros(int(self.RATE * 0.3), dtype=np.float32)
            single_full_loop = np.concatenate((single_pattern, silence))
            complete_wave = np.tile(single_full_loop, 4)
            
            speaker_scale = float(self.speaker_volume) / 100.0
            scaled_wave = (complete_wave * speaker_scale).astype(np.int16).tobytes()

            output_index = self._get_device_index(output_device_name, is_output=True)
            stream = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                output=True,
                output_device_index=output_index,
                frames_per_buffer=self.CHUNK
            )

            chunk_size = self.CHUNK
            for i in range(0, len(scaled_wave), chunk_size):
                if not getattr(self, 'is_playing_speaker_test', False):
                    break
                stream.write(scaled_wave[i:i+chunk_size])

            stream.stop_stream()
            stream.close()
            return {"success": True, "message": "Test speaker selesai / dihentikan."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def stop_speaker_test(self):
        self.is_playing_speaker_test = False
        return {"success": True}

    def _get_audio_level(self, data):
        """Menghitung level audio dalam persen dan dB."""
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)

        if samples.size == 0:
            return 0, -60.0

        rms = float(np.sqrt(np.mean(samples ** 2)))
        db = 20 * np.log10(max(rms / 32768.0, 1e-6))

        # Konversi rentang -60 sampai 0 dB menjadi 0 sampai 100%.
        percent = int(np.clip((db + 60) / 60 * 100, 0, 100))

        return percent, round(float(db), 1)

    def _send_mic_meter(self, data, mic_name=None):
        """Kirim level dB aktual ke meter Audio dan indikator VAD."""
        if not self.window:
            return
        try:
            percent, db = self._get_audio_level(data)
            settings = self.load_settings_from_json()
            threshold = float(settings.get("voiceThreshold", -40))
            is_passing = db >= threshold
            safe_name = json.dumps(mic_name or settings.get("inputDevice", "System default"))
            self.window.evaluate_js(
                f"window.updateMicMeter && window.updateMicMeter({percent}, {db}, {threshold}, {str(is_passing).lower()})"
            )
            self.window.evaluate_js(
                "window.updateVoiceActivityMonitorUI && "
                f"window.updateVoiceActivityMonitorUI({safe_name}, {db}, {str(is_passing).lower()})"
            )
        except Exception as e:
            print(f"[MIC METER ERROR] {e}")

    def update_voice_threshold(self, threshold):
        """Simpan threshold VAD agar slider langsung berlaku pada stream aktif."""
        try:
            threshold = max(-60.0, min(0.0, float(threshold)))
            settings = self.load_settings_from_json()
            settings["voiceThreshold"] = str(threshold)
            self.save_settings_to_json(settings)
            return {"success": True, "threshold": threshold}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def start_mic_test_record(self, input_device_name=None):
        try:
            if self.is_ptt_active or self.ptt_floor_requested or self.ptt_floor_granted:
                return {"success": False, "message": "PTT sedang aktif atau sedang meminta floor. Lepaskan PTT terlebih dahulu sebelum test microphone."}
            self._resume_stream_after_mic_test = bool(self.is_recording and self.load_settings_from_json().get("activationMode") in ("Voice Activation", "Continuous"))
            if self.is_recording:
                self.is_recording = False
                if self.recording_thread and self.recording_thread.is_alive():
                    self.recording_thread.join(timeout=1.5)
            if self.is_voice_activity_monitoring and self.voice_monitor_thread and self.voice_monitor_thread.is_alive():
                return {"success": False, "message": "Monitor VAD sedang aktif. Tutup tab Voice terlebih dahulu sebelum test microphone."}
            self.stop_audio_test()
            self.is_recording_mic_test = True
            self.test_audio_frames = []

            input_index = self._get_device_index(
                input_device_name,
                is_output=False
            )

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

                    print(f"[MIC TEST] Recording dimulai: {input_device_name}")

                    while self.is_recording_mic_test:
                        data = stream_in.read(
                            self.CHUNK,
                            exception_on_overflow=False
                        )

                        audio_data = np.frombuffer(
                            data,
                            dtype=np.int16
                        ).astype(np.float32)

                        mic_scale = float(self.mic_volume) / 100.0
                        processed = np.clip(
                            audio_data * mic_scale,
                            -32768,
                            32767
                        ).astype(np.int16).tobytes()

                        with self.audio_lock:
                            self.test_audio_frames.append(processed)

                        # Meter menggunakan audio asli sebelum pengaturan volume.
                        self._send_mic_meter(data, input_device_name)

                except Exception as e:
                    print(f"[MIC RECORD ERROR] {e}")

                    if self.window:
                        self.window.evaluate_js(
                            "window.updateMicMeter && window.updateMicMeter(0, -60, -40, false)"
                        )

                finally:
                    if stream_in:
                        try:
                            stream_in.stop_stream()
                            stream_in.close()
                        except Exception:
                            pass

                    self.is_recording_mic_test = False
                    if self._resume_stream_after_mic_test:
                        self._resume_stream_after_mic_test = False
                        self.start_background_recording_if_needed()

            self.mic_test_thread = threading.Thread(
                target=record_worker,
                daemon=True
            )
            self.mic_test_thread.start()

            return {"success": True}

        except Exception as e:
            self.is_recording_mic_test = False
            print(f"[MIC TEST ERROR] {e}")
            return {"success": False, "message": str(e)}

    def start_voice_activity_monitor(self):
        try:
            self.is_voice_activity_monitoring = True
            # Jika stream audio utama sedang aktif,
            # pengukuran dilakukan dari stream tersebut.
            if self.is_recording:
                return {
                    "success": True,
                    "message": "Menggunakan audio stream aktif"
                }
            if (
                self.voice_monitor_thread
                and self.voice_monitor_thread.is_alive()
            ):
                return {"success": True}

            settings = self.load_settings_from_json()
            input_device_name = settings.get(
                "inputDevice",
                "System default"
            )
            input_index = self._get_device_index(
                input_device_name,
                is_output=False
            )

            def monitor_worker():
                stream = None
                try:
                    stream = self.pyaudio_instance.open(
                        format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.RATE,
                        input=True,
                        input_device_index=input_index,
                        frames_per_buffer=self.CHUNK
                    )
                    print("[VOICE ACTIVITY] Monitor dimulai")
                    while (
                        self.is_voice_activity_monitoring
                        and not self.is_recording
                    ):
                        data = stream.read(
                            self.CHUNK,
                            exception_on_overflow=False
                        )
                        percent, db = self._get_audio_level(data)
                        threshold = float(
                            self.load_settings_from_json().get("voiceThreshold", -40)
                        )
                        is_passing = db >= threshold
                        if self.window:
                            payload = {
                                "micName": input_device_name,
                                "dbValue": db,
                                "isPassing": is_passing
                            }
                            self.window.evaluate_js(
                                "window.updateVoiceActivityMonitorUI && "
                                f"window.updateVoiceActivityMonitorUI("
                                f"{json.dumps(payload['micName'])}, "
                                f"{payload['dbValue']}, "
                                f"{str(payload['isPassing']).lower()}"
                                f")"
                            )
                except Exception as e:
                    print(f"[VOICE ACTIVITY ERROR] {e}")
                finally:
                    if stream:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except Exception:
                            pass
                    print("[VOICE ACTIVITY] Monitor berhenti")

            self.voice_monitor_thread = threading.Thread(
                target=monitor_worker,
                daemon=True
            )
            self.voice_monitor_thread.start()
            return {"success": True}

        except Exception as e:
            self.is_voice_activity_monitoring = False
            print(f"[VOICE ACTIVITY START ERROR] {e}")
            return {"success": False, "message": str(e)}


    def stop_voice_activity_monitor(self):
        self.is_voice_activity_monitoring = False

        if (
            self.voice_monitor_thread
            and self.voice_monitor_thread.is_alive()
        ):
            self.voice_monitor_thread.join(timeout=0.5)

        return {"success": True}

    def stop_mic_test_and_playback(self, output_device_name=None):
        try:
            self.is_recording_mic_test = False
            if hasattr(self, 'mic_test_thread') and self.mic_test_thread.is_alive():
                self.mic_test_thread.join(timeout=0.2)
            time.sleep(0.3)  
            
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

                    speaker_scale = float(self.speaker_volume) / 100.0
                    for data in frames:
                        if not getattr(self, 'is_playing_mic_test', False):
                            break
                        audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                        scaled_chunk = audio_chunk * speaker_scale
                        final_data = np.clip(scaled_chunk, -32768, 32767).astype(np.int16).tobytes()
                        stream_out.write(final_data)
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
        return {"success": True}

    def go_to_index(self):
        if self.window:
            base_path = os.path.dirname(os.path.abspath(__file__))
            index_path = os.path.join(base_path, 'frontend', 'index.html')
            file_url = f"file:///{index_path.replace(os.sep, '/')}"
            self.window.load_url(file_url)

    def go_to_login(self):
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

                if dev['max_input_channels'] > 0 and name not in seen_names:
                    input_devices.append({"id": dev['index'], "name": name})
                    seen_names.add(name)

                if dev['max_output_channels'] > 0:
                    if not any(d['name'] == name for d in output_devices):
                        output_devices.append({"id": dev['index'], "name": name})

            return {"success": True, "inputs": input_devices, "outputs": output_devices}
        except Exception as e:
            return {"success": False, "inputs": [], "outputs": []}

    def get_available_com_ports(self):
        try:
            ports = serial.tools.list_ports.comports()
            port_list = [{"device": p.device, "description": p.description} for p in ports]
            return {"success": True, "ports": port_list}
        except Exception as e:
            return {"success": False, "ports": [], "message": str(e)}

    def fetch_online_members(self):
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
        self.current_room_id = channel_id
        if self.current_user:
            self.current_user['current_room'] = channel_id
        if self.sio.connected:
            self.sio.emit('join_channel', str(channel_id))
            print(f"[DEBUG LOG - ROOM] Berhasil bergabung ke channel/room: {channel_id}")
        self.start_background_recording_if_needed()
        return self.roip_manager.set_active_channel(channel_id)

    def request_floor(self, room_id=None, user_id=None):
        if not self.sio.connected:
            return {
                "success": False,
                "message": "Socket server belum terhubung"
            }

        if not self.current_user:
            return {
                "success": False,
                "message": "User belum login"
            }

        user_id = user_id or self.current_user.get('id')
        room_id = room_id or self.current_room_id

        if not user_id or not room_id:
            return {
                "success": False,
                "message": "Room ID atau User ID tidak valid"
            }

        if self.ptt_floor_requested or self.ptt_floor_granted:
            return {
                "success": True,
                "message": "Permintaan floor sedang aktif"
            }

        self.ptt_floor_requested = True
        self.ptt_floor_granted = False
        self.ptt_cancel_pending = False
        self.ptt_request_room_id = str(room_id)

        payload = {
            'roomId': str(room_id),
            'userId': str(user_id),
            'username': (
                self.current_user.get('full_name')
                or self.current_user.get('fullname')
                or self.current_user.get('username')
                or 'Unknown User'
            )
        }

        print(f"[PTT FLOOR] Meminta floor: {payload}")

        self.sio.emit('request_floor', payload)

        return {
            "success": True,
            "message": "Permintaan floor dikirim"
        }

    def set_activation_mode(self, mode_name):
        """Hanya menyimpan mode audio dari halaman Settings."""
        allowed = ("Voice Activation", "Continuous", "Push to Talk")

        if mode_name not in allowed:
            return {
                "success": False,
                "message": "Mode aktivasi tidak valid"
            }

        try:
            settings = self.load_settings_from_json()
            settings["activationMode"] = mode_name
            self.save_settings_to_json(settings)

            # Hentikan stream jika sebelumnya masih aktif.
            self.is_recording = False

            if self.recording_thread and self.recording_thread.is_alive():
                self.recording_thread.join(timeout=2)

            # Lepaskan floor jika sebelumnya VAD sedang transmit.
            if self.vad_floor_requested or self.vad_floor_granted:
                if self.sio.connected and self.current_user:
                    self.sio.emit("release_floor", {
                        "roomId": str(
                            self.ptt_request_room_id or self.current_room_id
                        ),
                        "userId": str(self.current_user.get("id", ""))
                    })

            self.vad_floor_requested = False
            self.vad_floor_granted = False
            self.vad_voice_since = None
            self.vad_silence_since = None
            self.ptt_floor_requested = False
            self.ptt_floor_granted = False
            self.ptt_request_room_id = None
            self.is_ptt_active = False

            print(f"[SETTINGS] Mode audio disimpan: {mode_name}")
            print("[SETTINGS] Tidak menjalankan audio transmit dari halaman Settings.")

            return {
                "success": True,
                "mode": mode_name
            }

        except Exception as e:
            print(f"[SETTINGS ERROR] {e}")
            return {
                "success": False,
                "message": str(e)
            }

    def start_background_recording_if_needed(self):
        try:
            settings = self.load_settings_from_json()
            activation_mode = settings.get("activationMode", "Push to Talk")
            
            # Jangan jalankan background recording jika mode adalah Push to Talk!
            if activation_mode == "Push to Talk":
                return

            if activation_mode in ["Voice Activation", "Continuous"]:
                with self.audio_lock:
                    if not getattr(self, 'is_recording', False):
                        self.is_recording = True
                        self.ptt_start_time = time.time()
                        self.recording_thread = threading.Thread(target=self._record_and_stream_loop)
                        self.recording_thread.daemon = True
                        self.recording_thread.start()
        except Exception as e:
            print(f"[AUDIO ERROR] Gagal memulai background recording: {e}")

    def fetch_private_chats(self, target_user_id):
        if not self.auth_token:
            return {
                "success": False,
                "message": "Token tidak tersedia"
            }
        if not self.current_user or not self.current_user.get("id"):
            return {
                "success": False,
                "message": "User belum login"
            }
        user_id = self.current_user.get("id")
        headers = {
            "Authorization": f"Bearer {self.auth_token}"
        }
        try:
            url = (
                f"{self.api_base_url}/private-messages/"
                f"{user_id}/{target_user_id}"
            )
            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )
            return response.json()
        except Exception as e:
            print(f"[PRIVATE CHAT ERROR] {e}")

            return {
                "success": False,
                "message": str(e)
            }
    
    def fetch_channel_chats(self, room_id):
        if not self.auth_token:
            return {"success": False, "message": "Token tidak tersedia"}
        headers = {"Authorization": f"Bearer {self.auth_token}"}
        try:
            url = f"{self.api_base_url}/channels/{room_id}/chats"
            response = requests.get(url, headers=headers, timeout=5)
            return response.json()
        except Exception as e:
            return {"success": False, "message": str(e)}

    def start_ptt(self):
        print("[DEBUG LOG - PTT] Memulai audio setelah floor granted...")
        with self.audio_lock:
            if self.is_recording:
                return {"success": True, "message": "Audio sudah aktif"}
            self.is_recording = True
            self.is_ptt_active = True
            self.ptt_start_time = time.time()
            if not self.recording_thread or not self.recording_thread.is_alive():
                self.recording_thread = threading.Thread(target=self._record_and_stream_loop, daemon=True)
                self.recording_thread.start()
        return {"success": True}

    def _record_and_stream_loop(self):
        stream = None
        try:
            settings = self.load_settings_from_json()
            input_device_name = settings.get("inputDevice", "System default")
            input_index = self._get_device_index(input_device_name, is_output=False)
            stream = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                input_device_index=input_index,
                frames_per_buffer=self.CHUNK
            )

            while getattr(self, 'is_recording', False):
                try:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                except Exception as e:
                    print(f"[AUDIO READ ERROR] {e}")
                    continue
                if not data:
                    continue

                settings = self.load_settings_from_json()
                mode = settings.get("activationMode", "Push to Talk")
                threshold = float(settings.get("voiceThreshold", -40))
                percent, db = self._get_audio_level(data)
                is_passing = db >= threshold

                # Meter dB selalu mengukur sinyal mic asli, sebelum micVolume.
                if self.window and (self.is_voice_activity_monitoring or mode in ["Voice Activation", "Continuous"] or self.is_ptt_active):
                    try:
                        self._send_mic_meter(data, input_device_name)
                    except Exception as e:
                        print(f"[VOICE METER ERROR] {e}")

                if not self.sio.connected or getattr(self, 'is_mic_muted', False):
                    continue

                # Voice Activation: minta floor saat suara melewati threshold,
                # lalu lepaskan setelah senyap stabil (hangover 700 ms).
                if mode == "Voice Activation":
                    if is_passing:
                        self.vad_silence_since = None
                        if (not self.vad_floor_requested and not self.vad_floor_granted
                                and not self.ptt_floor_requested and not self.ptt_floor_granted
                                and time.time() - self.vad_last_request_at >= 1.5):
                            user_id = (self.current_user or {}).get("id")
                            room_id = self.current_room_id
                            if user_id and room_id:
                                self.ptt_floor_requested = True
                                self.vad_floor_requested = True
                                self.vad_last_request_at = time.time()
                                self.ptt_request_room_id = str(room_id)
                                self.sio.emit('request_floor', {
                                    'roomId': str(room_id),
                                    'userId': str(user_id),
                                    'username': (
                                        self.current_user.get('full_name')
                                        or self.current_user.get('fullname')
                                        or self.current_user.get('username')
                                        or 'Unknown User'
                                    )
                                })
                                print(f"[VAD] Meminta floor room={room_id}")
                    else:
                        if self.vad_floor_granted:
                            if self.vad_silence_since is None:
                                self.vad_silence_since = time.time()
                            elif time.time() - self.vad_silence_since >= self.vad_hangover_seconds:
                                user_id = (self.current_user or {}).get("id")
                                room_id = self.ptt_request_room_id or self.current_room_id
                                self.sio.emit('release_floor', {
                                    'roomId': str(room_id),
                                    'userId': str(user_id or '')
                                })
                                self.vad_floor_granted = False
                                self.vad_floor_requested = False
                                self.ptt_floor_granted = False
                                self.ptt_floor_requested = False
                                self.ptt_request_room_id = None
                                self.vad_silence_since = None
                                print("[VAD] Senyap terdeteksi; floor dilepaskan")
                    should_transmit = self.vad_floor_granted
                elif mode == "Continuous":
                    should_transmit = True
                else:
                    # PTT manual hanya mengirim setelah server memberikan floor.
                    should_transmit = self.is_ptt_active and self.ptt_floor_granted

                if not should_transmit:
                    continue

                mic_scale = float(self.mic_volume) / 100.0
                audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                processed_data = np.clip(audio_chunk * mic_scale, -32768, 32767).astype(np.int16).tobytes()
                self.sio.emit('audio_stream', processed_data)

        except Exception as e:
            print(f"[ERROR PyAudio Stream] {e}")
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def release_ptt(self, room_id=None, user_id=None):
        print("[PTT] Menghentikan audio stream...")

        with self.audio_lock:
            self.is_recording = False
            self.is_ptt_active = False

        room_id = room_id or self.ptt_request_room_id or self.current_room_id

        user_id = user_id or (
            self.current_user.get('id') if self.current_user else None
        )

        # Jika belum mendapat floor, tandai pembatalan.
        if self.ptt_floor_requested and not self.ptt_floor_granted:
            self.ptt_cancel_pending = True

        if self.sio.connected and (
            self.ptt_floor_requested or self.ptt_floor_granted
        ):
            payload = {
                'roomId': str(room_id),
                'userId': str(user_id or '')
            }

            print(f"[PTT FLOOR] Mengirim release_floor: {payload}")

            self.sio.emit('release_floor', payload)

        duration_seconds = max(
            1,
            int(time.time() - self.ptt_start_time)
        )

        self.ptt_floor_requested = False
        self.ptt_floor_granted = False
        self.ptt_request_room_id = None

        return {
            "success": True,
            "duration": duration_seconds
        }

    def process_incoming_audio_stream(self, raw_audio_data):
        try:
            speaker_scale = float(self.speaker_volume) / 100.0
            audio_chunk = np.frombuffer(raw_audio_data, dtype=np.int16).astype(np.float32)
            scaled_chunk = audio_chunk * speaker_scale
            return np.clip(scaled_chunk, -32768, 32767).astype(np.int16).tobytes()
        except Exception as e:
            return raw_audio_data

    def handle_incoming_channel_audio(self, channel_id, raw_audio_data):
        try:
            processed_audio = self.process_incoming_audio_stream(raw_audio_data)
            settings = self.load_settings_from_json()
            output_device_name = settings.get("outputDevice", "System default")
            output_index = self._get_device_index(output_device_name, is_output=True)

            stream_out = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                output=True,
                output_device_index=output_index,
                frames_per_buffer=self.CHUNK
            )
            stream_out.write(processed_audio)
            stream_out.stop_stream()
            stream_out.close()
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}