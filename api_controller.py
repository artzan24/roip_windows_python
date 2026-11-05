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

        self.mic_volume = 100
        self.speaker_volume = 100

        # --- INISIALISASI STATE CONTROL ---
        self.is_mic_muted = False
        self.is_speaker_muted = False
        self.was_transmitting = False
        self.ptt_start_time = 0
        self.is_ptt_active = False
        
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
            # [PENTING] Daftarkan socket user ke server agar private chat dan status online berfungsi
            if self.current_user and self.current_user.get("id"):
                user_id = self.current_user.get("id")
                self.sio.emit('register_user_socket', user_id)
                print(f"[SOCKET.IO] Mengirim event register_user_socket untuk User ID: {user_id}")

        # Event untuk menerima pesan chat channel
        @self.sio.on('receive_channel_chat')
        def on_receive_channel_chat(data):
            print("[CHANNEL CHAT] Pesan channel diterima:", data)
            if self.window:
                try:
                    import json
                    safe_data = json.dumps(data)
                    # Meneruskan data chat ke fungsi JavaScript frontend (misal: displayChannelMessage)
                    self.window.evaluate_js(f"if(typeof displayChannelMessage === 'function') {{ displayChannelMessage({safe_data}); }}")
                except Exception as e:
                    print("Gagal mengevaluasi JS receive_channel_chat:", e)

        # Event untuk menerima status PTT/transmisi dari user lain
        @self.sio.on('user_ptt_broadcast')
        def on_user_ptt_broadcast(data):
            print("[PTT BROADCAST] Status PTT user lain diterima:", data)
            if self.window:
                try:
                    import json
                    safe_data = json.dumps(data)
                    # Meneruskan status PTT/transmisi ke fungsi JavaScript frontend (misal: updatePeerPTTStatus)
                    self.window.evaluate_js(f"if(typeof updatePeerPTTStatus === 'function') {{ updatePeerPTTStatus({safe_data}); }}")
                except Exception as e:
                    print("Gagal mengevaluasi JS user_ptt_broadcast:", e)

        # Event untuk menerima update status floor dari server pusat
        @self.sio.on('floor_status_update')
        def on_floor_status_update(data):
            print("[FLOOR STATUS] Update floor room diterima:", data)
            if self.window:
                try:
                    import json
                    safe_data = json.dumps(data)
                    self.window.evaluate_js(f"if(typeof updateFloorStatusUI === 'function') {{ updateFloorStatusUI({safe_data}); }}")
                except Exception as e:
                    print("Gagal mengevaluasi JS floor_status_update:", e)

        @self.sio.event
        def disconnect():
            print("[SOCKET.IO] Terputus dari WebSocket server pusat.")

        @self.sio.on('floor_granted')
        def on_floor_granted(data):
            print("[PTT] Floor diizinkan oleh server:", data)

        @self.sio.on('floor_denied')
        def on_floor_denied(data):
            print("[PTT] Floor ditolak oleh server:", data)

        @self.sio.on('receive_private_chat')
        def on_receive_private_chat(data):
            print("[PRIVATE CHAT] Pesan masuk diterima:", data)
            if self.window:
                try:
                    import json
                    safe_data = json.dumps(data)
                    self.window.evaluate_js(f"if(typeof displayPrivateMessage === 'function') {{ displayPrivateMessage({safe_data}); }}")
                except Exception as e:
                    print("Gagal mengevaluasi JS receive_private_chat:", e)

        @self.sio.on('update_user_connection')
        def on_update_user_connection(data):
            print("[SOCKET.IO] Update koneksi user diterima:", data)
            if self.window:
                try:
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

        @self.sio.on('audio_stream_incoming')
        def on_audio_stream_incoming(data):
            # data bisa berupa dictionary atau bytes mentah dari server, sesuaikan dengan backend Node.js Anda
            # Contoh jika server mengirimkan channel_id dan audio buffer
            try:
                channel_id = data.get('roomId') or self.current_room_id
                audio_bytes = data.get('audioData') # atau langsung bytes jika dikirim binary
                
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
            except Exception as e:
                print(f"[SOCKET.IO ERROR] Gagal terhubung ke {server_url}: {e}")

    def set_window(self, window):
        self.window = window

    def check_server_status(self):
        """
        Mengecek status kesehatan server pusat secara dinamis berdasarkan 
        IP & Port dari config.json dan melakukan uji koneksi socket aktual.
        """
        try:
            # Muat konfigurasi terbaru dari config.json
            config = self.load_config()
            server_ip = config.get("ip_server", "10.18.200.21")
            server_port = int(config.get("port", 3002))

            # 1. Uji koneksi socket langsung ke server pusat
            socket.create_connection((server_ip, server_port), timeout=2)
            return {
                "success": True, 
                "message": f"Terhubung ke server pusat {server_ip}:{server_port}"
            }
        except OSError:
            try:
                # 2. Fallback cadangan: cek koneksi internet umum (Google DNS) jika server pusat belum merespons
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
                
            # Sinkronkan nilai volume global secara otomatis
            self._load_volumes_from_settings()
            
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

        if os.path.exists(SETTINGS_FILE):
            try:
                if os.path.getsize(SETTINGS_FILE) == 0:
                    return default_settings

                # Gunakan encoding 'utf-8-sig' untuk mencegah error karakter tersembunyi/BOM
                with open(SETTINGS_FILE, 'r', encoding='utf-8-sig') as f:
                    content = f.read().strip()
                    if not content:
                        return default_settings
                    
                    saved_data = json.loads(content)
                    if isinstance(saved_data, dict):
                        default_settings.update(saved_data)
                    return default_settings
            except Exception as e:
                print(f"[SETTINGS ERROR] Gagal membaca settings.json (menggunakan default): {e}")
                
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
            # Simpan juga ke settings.json agar persisten
            settings = self.load_settings_from_json()
            settings["micVolume"] = str(self.mic_volume)
            settings["speakerVolume"] = str(self.speaker_volume)
            self.save_settings_to_json(settings)
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _get_device_index(self, device_name, is_output=False):
        """Mencari index device secara aman, kompatibel antara nama pilihan UI dan PyAudio."""
        if not device_name or device_name == "System default" or device_name.strip() == "":
            print("[AUDIO INFO] Menggunakan perangkat System default.")
            return None
        
        try:
            # Cari langsung berdasarkan kecocokan nama di PyAudio agar indeksnya 100% akurat untuk streaming
            device_count = self.pyaudio_instance.get_device_count()
            for i in range(device_count):
                dev_info = self.pyaudio_instance.get_device_info_by_index(i)
                dev_name = dev_info.get('name', '')
                
                channels = dev_info.get('maxOutputChannels') if is_output else dev_info.get('maxInputChannels')
                if channels > 0:
                    # Cocokkan nama dari pengaturan dengan hardware PyAudio
                    if device_name.strip().lower() in dev_name.strip().lower() or dev_name.strip().lower() in device_name.strip().lower():
                        print(f"[AUDIO INFO] Perangkat PyAudio cocok: '{dev_name}' pada indeks {i}")
                        return i
        except Exception as e:
            print(f"[ERROR] Gagal mencocokkan device index PyAudio: {e}")
        
        # Fallback ke System Default jika tidak ditemukan
        print(f"[AUDIO WARNING] Perangkat '{device_name}' tidak ditemukan. Fallback ke System default.")
        return None

    def test_speaker(self, output_device_name=None):
        """Memutar melodi tes audio berdurasi panjang (~10-12 detik) dengan opsi stop manual."""
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

            # Potong chunk kecil-kecil agar bisa mendeteksi perintah STOP secara real-time
            chunk_size = self.CHUNK
            for i in range(0, len(scaled_wave), chunk_size):
                if not getattr(self, 'is_playing_speaker_test', False):
                    break
                chunk = scaled_wave[i:i+chunk_size]
                stream.write(chunk)

            stream.stop_stream()
            stream.close()
            return {"success": True, "message": "Test speaker selesai / dihentikan."}
        except Exception as e:
            print(f"[SPEAKER TEST ERROR] {e}")
            return {"success": False, "message": str(e)}

    def stop_speaker_test(self):
        """Menghentikan tes speaker secara manual."""
        self.is_playing_speaker_test = False
        return {"success": True}

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
                        
                        # Terapkan Microphone Volume pada data rekaman test mic & meteran
                        mic_scale = float(self.mic_volume) / 100.0
                        audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                        scaled_audio_chunk = audio_data * mic_scale
                        
                        processed_data = np.clip(scaled_audio_chunk, -32768, 32767).astype(np.int16).tobytes()
                        
                        with self.audio_lock:
                            self.test_audio_frames.append(processed_data)
                        
                        if chunk_count % 3 == 0:
                            try:
                                if len(audio_data) > 0:
                                    rms = np.sqrt(np.mean(scaled_audio_chunk**2))
                                    
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
                                    
                                    if self.window:
                                        self.window.evaluate_js(f"if(typeof updateMicMeter === 'function') {{ updateMicMeter({level}); }}")
                            except Exception as ex:
                                pass

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

                    # Ambil skala Speaker Volume saat playback test mic
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
        if self.window:
            try:
                self.window.evaluate_js("if(typeof updateMicMeter === 'function') { updateMicMeter(0); }")
            except:
                pass
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
            self.is_monitoring_voice = False
            
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

    def get_available_com_ports(self):
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
            
        # [PENTING] Pastikan background recording langsung berjalan otomatis 
        # jika mode aktif adalah Voice Activation atau Continuous
        self.start_background_recording_if_needed()
            
        return self.roip_manager.set_active_channel(channel_id)

    def start_background_recording_if_needed(self):
        """Memulai rekaman otomatis di background jika mode bukan Push to Talk"""
        try:
            settings = self.load_settings_from_json()
            activation_mode = settings.get("activationMode", "Voice Activation")
            
            # Jika mode Voice Activation atau Continuous, jalankan thread rekam otomatis
            if activation_mode in ["Voice Activation", "Continuous"]:
                with self.audio_lock:
                    if not getattr(self, 'is_recording', False):
                        self.is_recording = True
                        self.ptt_start_time = time.time()
                        
                        # Jalankan thread rekam dan streaming
                        self.recording_thread = threading.Thread(target=self._record_and_stream_loop)
                        self.recording_thread.daemon = True
                        self.recording_thread.start()
                        print(f"[AUDIO] Background recording dimulai otomatis untuk mode: {activation_mode}")
            else:
                # Jika mode Push to Talk, pastikan rekaman berhenti jika sebelumnya menyala otomatis
                if getattr(self, 'is_recording', False) and not getattr(self, 'is_ptt_active', False):
                    self.is_recording = False
        except Exception as e:
            print(f"[AUDIO ERROR] Gagal memulai background recording: {e}")

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

    # --- 4. MANAJEMEN PTT & SOCKET.IO STREAMING (DENGAN PENERAPAN GLOBAL MIC VOLUME) ---
    def start_ptt(self):
        print("[PTT] Transmisi mulai (Streaming via Socket.io)...")
        
        with self.audio_lock:
            self.is_recording = True
            self.is_ptt_active = True  # Set status PTT aktif
            self.ptt_start_time = time.time()

        try:
            settings = self.load_settings_from_json()
            hw_interface = settings.get("hardwareInterface", {})
            is_hardware_enabled = hw_interface.get("enableHardwarePTT", False)
            com_port = hw_interface.get("serialPort")
            ptt_pin = hw_interface.get("pttPin", "RTS").upper()
            baud_rate = int(hw_interface.get("baudRate", 9600))

            available_ports = [p.device for p in serial.tools.list_ports.comports()]

            if is_hardware_enabled and com_port in available_ports:
                self.ser_ptt = serial.Serial(com_port, baud_rate, timeout=1)
                if ptt_pin == "DTR":
                    self.ser_ptt.setDTR(True)
                else:
                    self.ser_ptt.setRTS(True)
                print(f"[HARDWARE PTT] Port {com_port} terdeteksi dan AKTIF.")
            else:
                if is_hardware_enabled:
                    print(f"[HARDWARE PTT INFO] Port {com_port} tidak terdeteksi. Menggunakan mode software murni.")
                else:
                    print("[HARDWARE PTT] Dinonaktifkan pada pengaturan.")
        except Exception as e:
            print(f"[HARDWARE PTT WARNING] Gagal menginisialisasi port serial: {e}")

        if self.sio.connected:
            user_id = self.current_user.get("id") if self.current_user else 1
            username = self.current_user.get("full_name", "Dispatcher")
            self.sio.emit('request_floor', {
                'roomId': str(self.current_room_id),
                'userId': user_id,
                'username': username
            })

        # [PERBAIKAN UTAMA] Paksa matikan thread lama jika ada, lalu buat thread rekam baru agar stream segar
        if hasattr(self, 'recording_thread') and self.recording_thread is not None:
            try:
                if self.recording_thread.is_alive():
                    # Beri waktu sebentar atau biarkan loop mendeteksi is_recording
                    pass
            except:
                pass

        self.recording_thread = threading.Thread(target=self._record_and_stream_loop)
        self.recording_thread.daemon = True
        self.recording_thread.start()
        print("[PTT] Thread rekam & streaming berhasil dimulai.")
        
        return {"success": True}

    def _record_and_stream_loop(self):
        """Looping menangkap stream audio dengan penundaan keheningan (debounce) untuk Voice Activation & Continuous."""
        stream = None
        try:
            settings = self.load_settings_from_json()
            input_device_name = settings.get("inputDevice", "System default")
            input_index = self._get_device_index(input_device_name, is_output=False)
            
            print(f"[AUDIO STREAM] Membuka input stream untuk: '{input_device_name}' (Index: {input_index})")

            stream = self.pyaudio_instance.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                input_device_index=input_index,
                frames_per_buffer=self.CHUNK
            )
            
            self.was_transmitting = False
            user_id = self.current_user.get("id") if self.current_user else 1
            username = self.current_user.get("full_name", "Dispatcher")

            # Variabel Debounce / Grace Period untuk mencegah terpotong tiap 1 detik
            silence_start_time = None
            SILENCE_DELAY_SECONDS = 2  # Waktu tunggu (detik) sebelum benar-mera menutup rekaman

            while getattr(self, 'is_recording', False):
                try:
                    data = stream.read(self.CHUNK, exception_on_overflow=False)
                except Exception:
                    continue

                if not data or not self.sio.connected:
                    continue

                if getattr(self, 'is_mic_muted', False):
                    continue

                settings = self.load_settings_from_json()
                activation_mode = settings.get("activationMode", "Voice Activation")
                threshold_db = float(settings.get("voiceThreshold", -40))

                mic_scale = float(self.mic_volume) / 100.0
                audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                scaled_audio_chunk = audio_chunk * mic_scale
                
                rms = np.sqrt(np.mean(scaled_audio_chunk**2)) if len(scaled_audio_chunk) > 0 else 0
                db_value = (20 * np.log10(rms / 32768.0)) if rms > 0 else -100.0
                db_value = max(-60.0, min(0.0, db_value))

                # Evaluasi kondisi mentah berdasarkan suara
                raw_transmitting = False
                if activation_mode == "Continuous":
                    raw_transmitting = True
                elif activation_mode == "Push to Talk":
                    raw_transmitting = getattr(self, 'is_ptt_active', False)
                elif activation_mode == "Voice Activation":
                    if db_value >= threshold_db:
                        raw_transmitting = True

                # --- LOGIKA DEBOUNCE / HYSTERESIS UNTUK VOICE ACTIVATION ---
                if activation_mode == "Voice Activation":
                    if raw_transmitting:
                        # Jika suara terdeteksi, reset waktu senyap dan tandai sedang aktif
                        silence_start_time = None
                        is_transmitting = True
                    else:
                        # Jika suara di bawah threshold (senyap)
                        if self.was_transmitting:
                            if silence_start_time is None:
                                silence_start_time = time.time()
                            
                            # Cek apakah durasi keheningan sudah melewati batas tunda (misal 1.5 detik)
                            if time.time() - silence_start_time >= SILENCE_DELAY_SECONDS:
                                is_transmitting = False
                            else:
                                # Selama masa tenggang tunda, tetap anggap transmitting agar tidak terpotong
                                is_transmitting = True
                        else:
                            is_transmitting = False
                else:
                    is_transmitting = raw_transmitting

                # --- SINKRONISASI FLOOR KE SERVER ---
                if is_transmitting and not self.was_transmitting:
                    self.ptt_start_time = time.time()
                    if self.sio.connected:
                        self.sio.emit('request_floor', {
                            'roomId': str(self.current_room_id),
                            'userId': user_id,
                            'username': username
                        })
                        print(f"[VOICE ACT] Suara aktif ({db_value:.1f} dB). Membuka floor...")

                elif not is_transmitting and self.was_transmitting:
                    duration_seconds = int(time.time() - self.ptt_start_time)
                    duration_seconds = max(1, duration_seconds)
                    if self.sio.connected:
                        self.sio.emit('release_floor', {
                            'roomId': str(self.current_room_id),
                            'userId': user_id,
                            'duration': duration_seconds
                        })
                        print(f"[VOICE ACT] Sesi selesai. Total Durasi: {duration_seconds} detik.")

                self.was_transmitting = is_transmitting

                if self.window:
                    try:
                        self.window.evaluate_js(f"updateVoiceActivityIndicator({db_value:.1f}, {1 if is_transmitting else 0})")
                    except:
                        pass

                if not is_transmitting:
                    time.sleep(0.01)
                    continue

                processed_data = np.clip(scaled_audio_chunk, -32768, 32767).astype(np.int16).tobytes()
                self.sio.emit('audio_stream', processed_data)
            
            if stream:
                stream.stop_stream()
                stream.close()
            print("[AUDIO STREAM] Stream background ditutup.")
        except Exception as e:
            print(f"[ERROR PyAudio Stream] Gagal merekam/streaming: {e}")
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except:
                    pass

    def set_mic_muted(self, is_muted):
        self.is_mic_muted = is_muted
        if is_muted:
            self.is_recording = False
        print(f"[AUDIO] Status Global Mic Mute: {is_muted}")
        return {"success": True}

    def set_speaker_muted(self, is_muted):
        self.is_speaker_muted = is_muted
        # Jika speaker dimute (deafen), hentikan atau abaikan pemutaran audio masuk dari socket
        print(f"[AUDIO] Status Global Speaker Deafen: {is_muted}")
        return {"success": True}

    def start_voice_activity_monitor(self):
        """Memulai pemantauan mic khusus untuk indikator real-time dengan penanganan error yang aman."""
        import threading
        
        # Hentikan monitor sebelumnya jika sudah aktif agar tidak terjadi bentrok stream
        self.is_monitoring_voice = False
        time.sleep(0.1)
        
        def monitor_loop():
            stream = None
            try:
                settings = self.load_settings_from_json()
                input_device_name = settings.get("inputDevice", "System default")
                
                # Validasi aman untuk menghindari string invalid seperti 'master'
                if not input_device_name or str(input_device_name).lower() in ["system default", "master", ""]:
                    input_index = None
                else:
                    input_index = self._get_device_index(input_device_name, is_output=False)
                    
                threshold_db = float(settings.get("voiceThreshold", -40))

                dev_display_name = input_device_name
                if input_index is not None:
                    try:
                        dev_info = self.pyaudio_instance.get_device_info_by_index(input_index)
                        dev_display_name = dev_info.get('name', input_device_name)
                    except Exception:
                        input_index = None

                stream = self.pyaudio_instance.open(
                    format=self.FORMAT,
                    channels=self.CHANNELS,
                    rate=self.RATE,
                    input=True,
                    input_device_index=input_index,
                    frames_per_buffer=self.CHUNK
                )

                self.is_monitoring_voice = True
                
                while getattr(self, 'is_monitoring_voice', False):
                    try:
                        data = stream.read(self.CHUNK, exception_on_overflow=False)
                        if not data or not getattr(self, 'is_monitoring_voice', False):
                            break

                        audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                        rms = np.sqrt(np.mean(audio_chunk**2)) if len(audio_chunk) > 0 else 0
                        
                        if rms > 0:
                            db_value = 20 * np.log10(rms / 32768.0)
                        else:
                            db_value = -100.0

                        db_value = max(-60.0, min(0.0, db_value))
                        is_passing = db_value >= threshold_db

                        if self.window and getattr(self, 'is_monitoring_voice', False):
                            # Escape string nama device untuk keamanan JS syntax
                            safe_name = str(dev_display_name).replace("'", "\\'")
                            js_code = f"if(typeof updateVoiceActivityMonitorUI === 'function') {{ updateVoiceActivityMonitorUI('{safe_name}', {db_value:.1f}, {1 if is_passing else 0}); }}"
                            self.window.evaluate_js(js_code)
                    except Exception:
                        break

            except Exception as e:
                print(f"[MONITOR ERROR BERHASIL DIATASI] {e}")
            finally:
                if stream:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                
                # Reset UI indikator saat monitor berhenti
                if self.window:
                    try:
                        self.window.evaluate_js("if(typeof updateVoiceActivityMonitorUI === 'function') { updateVoiceActivityMonitorUI('System default', -100.0, 0); }")
                    except Exception:
                        pass

        if not getattr(self, 'is_monitoring_voice', False):
            threading.Thread(target=monitor_loop, daemon=True).start()

        return {"success": True, "message": "Voice monitor started."}

    def stop_voice_activity_monitor(self):
        """Menghentikan pemantauan mic dengan aman."""
        self.is_monitoring_voice = False
        return {"success": True}

    def release_ptt(self, room_id=1, user_id=1):
        print("[PTT] Transmisi berhenti. Menghentikan streaming...")
        self.is_recording = False
        self.is_ptt_active = False  # [PENTING] Matikan status PTT aktif
        
        if self.recording_thread and self.recording_thread.is_alive():
            self.recording_thread.join(timeout=1.0)

        duration_seconds = int(time.time() - getattr(self, 'ptt_start_time', time.time()))
        duration_seconds = max(1, duration_seconds)

        try:
            if hasattr(self, 'ser_ptt') and self.ser_ptt and self.ser_ptt.is_open:
                settings = self.load_settings_from_json()
                hw_interface = settings.get("hardwareInterface", {})
                ptt_pin = hw_interface.get("pttPin", "RTS").upper()

                if ptt_pin == "DTR":
                    self.ser_ptt.setDTR(False)
                else:
                    self.ser_ptt.setRTS(False)

                self.ser_ptt.close()
                print("[HARDWARE PTT] Pin PTT dimatikan & Port ditutup.")
        except Exception as e:
            print(f"[HARDWARE PTT ERROR] Gagal mematikan pin PTT: {e}")

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

    # --- FUNGSI GLOBAL UNTUK AUDIO MASUK (INCOMING STREAM DARI USER LAIN) ---
    def process_incoming_audio_stream(self, raw_audio_data):
        """
        Fungsi bantu untuk menerapkan global Speaker Volume pada audio masuk 
        dari user lain di room yang sama sebelum diputar ke speaker.
        """
        try:
            speaker_scale = float(self.speaker_volume) / 100.0
            audio_chunk = np.frombuffer(raw_audio_data, dtype=np.int16).astype(np.float32)
            scaled_chunk = audio_chunk * speaker_scale
            return np.clip(scaled_chunk, -32768, 32767).astype(np.int16).tobytes()
        except Exception as e:
            print(f"[INCOMING AUDIO ERROR] {e}")
            return raw_audio_data

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

    def handle_incoming_channel_audio(self, channel_id, raw_audio_data):
        """
        Menangani audio masuk dari channel tertentu.
        Jika channel tersebut sedang di-mute oleh user, abaikan audio.
        """
        try:
            # 1. Cek status audio channel melalui roip_manager apakah di-mute (speaker merah)
            is_channel_audio_enabled = True
            if hasattr(self, 'roip_manager') and hasattr(self.roip_manager, 'get_channel_audio_state'):
                is_channel_audio_enabled = self.roip_manager.get_channel_audio_state(channel_id)
            
            if not is_channel_audio_enabled:
                # Channel sedang dimute (ikon speaker merah), abaikan audio masuk!
                return {"success": False, "message": "Channel is muted"}

            # 2. Proses pemutaran audio dengan speaker volume global yang sudah kita buat
            processed_audio = self.process_incoming_audio_stream(raw_audio_data)
            
            # 3. Putar langsung ke speaker aktif menggunakan PyAudio
            settings = self.load_settings_from_json()
            output_device_name = settings.get("outputDevice", "System default")
            output_index = self._get_device_index(output_device_name, is_output=True)

            # Buka stream pemutaran singkat untuk chunk suara yang masuk
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
            print(f"[INCOMING CHANNEL AUDIO ERROR] {e}")
            return {"success": False, "message": str(e)}