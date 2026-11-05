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
            if self.current_user and self.current_user.get("id"):
                user_id = self.current_user.get("id")
                self.sio.emit('register_user_socket', user_id)
                print(f"[SOCKET.IO] Mengirim event register_user_socket untuk User ID: {user_id}")

        # --- EVENT LIVE CHAT CHANNEL ---
        @self.sio.on('receive_channel_chat')
        def on_receive_channel_chat(data):
            print(f"[DEBUG LOG - CHAT CHANNEL] Pesan diterima dari server: {data}")
            if self.window:
                try:
                    safe_data = json.dumps(data)
                    self.window.evaluate_js(f"if(typeof displayChannelMessage === 'function') {{ displayChannelMessage({safe_data}); }}")
                except Exception as e:
                    print(f"[ERROR LOG] Gagal mengevaluasi JS receive_channel_chat: {e}")

        # --- EVENT LIVE CHAT PRIVATE ---
        @self.sio.on('receive_private_chat')
        def on_receive_private_chat(data):
            print(f"[DEBUG LOG - PRIVATE CHAT] Pesan pribadi diterima: {data}")
            if self.window:
                try:
                    safe_data = json.dumps(data)
                    self.window.evaluate_js(f"if(typeof displayPrivateMessage === 'function') {{ displayPrivateMessage({safe_data}); }}")
                except Exception as e:
                    print(f"[ERROR LOG] Gagal mengevaluasi JS receive_private_chat: {e}")

        # --- EVENT PTT & STATUS BUSY/READY ---
        @self.sio.on('user_ptt_broadcast')
        def on_user_ptt_broadcast(data):
            print(f"[DEBUG LOG - PTT BROADCAST] Status PTT user lain diterima: {data}")
            if self.window:
                try:
                    safe_data = json.dumps(data)
                    self.window.evaluate_js(f"if(typeof updatePeerPTTStatus === 'function') {{ updatePeerPTTStatus({safe_data}); }}")
                except Exception as e:
                    print(f"[ERROR LOG] Gagal mengevaluasi JS user_ptt_broadcast: {e}")

        @self.sio.event
        def disconnect():
            print("[SOCKET.IO] Terputus dari WebSocket server pusat.")

        @self.sio.on('floor_granted')
        def on_floor_granted(data):
            print(f"[DEBUG LOG - PTT FLOOR] Floor diizinkan oleh server: {data}")

        @self.sio.on('floor_denied')
        def on_floor_denied(data):
            print(f"[DEBUG LOG - PTT FLOOR] Floor DITOLAK (BUSY) oleh server: {data}")

        @self.sio.on('floor_released')
        def on_floor_released(data):
            print(f"[DEBUG LOG - PTT FLOOR] Floor dilepas (READY): {data}")

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
                    while getattr(self, 'is_recording_mic_test', False):
                        data = stream_in.read(self.CHUNK, exception_on_overflow=False)
                        mic_scale = float(self.mic_volume) / 100.0
                        audio_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                        scaled_audio_chunk = audio_data * mic_scale
                        processed_data = np.clip(scaled_audio_chunk, -32768, 32767).astype(np.int16).tobytes()
                        
                        with self.audio_lock:
                            self.test_audio_frames.append(processed_data)
                except Exception as e:
                    print(f"[MIC RECORD ERROR] {e}")
                finally:
                    if stream_in:
                        try:
                            stream_in.stop_stream()
                            stream_in.close()
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
                except Exception:
                    continue

                if not data or not self.sio.connected or getattr(self, 'is_mic_muted', False):
                    continue

                mic_scale = float(self.mic_volume) / 100.0
                audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                scaled_audio_chunk = audio_chunk * mic_scale
                processed_data = np.clip(scaled_audio_chunk, -32768, 32767).astype(np.int16).tobytes()
                
                self.sio.emit('audio_stream', processed_data)
        except Exception as e:
            print(f"[ERROR PyAudio Stream] {e}")
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except:
                    pass

    def release_ptt(self, room_id=None, user_id=None):
        print("[DEBUG LOG - PTT] Menghentikan audio stream...")
        with self.audio_lock:
            self.is_recording = False
            self.is_ptt_active = False
        duration_seconds = max(1, int(time.time() - self.ptt_start_time))
        return {"success": True, "duration": duration_seconds}

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