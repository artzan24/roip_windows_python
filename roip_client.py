from database_manager import DatabaseManager

class RoIPClientManager:
    """
    Modul untuk menangani status operasional RoIP (PTT, Busy State, Mute, & Audio Channel).
    """
    def __init__(self):
        self.is_ptt_active = False       # Status global apakah ada user yang sedang PTT (Busy state)
        self.active_channel_id = None    # ID channel yang sedang aktif dipilih user
        self.is_global_muted = False     # Status mute global (footer sidebar aplikasi)
        self.channel_audio_states = {}   # Status pengaktifan suara per channel (ID Channel -> Boolean)

    def set_active_channel(self, channel_id):
        """Mengatur channel yang sedang aktif dipilih oleh operator."""
        self.active_channel_id = channel_id
        return {"success": True, "active_channel": channel_id}

    def request_ptt(self, user_id, channel_id):
        """Mengecek status jalur komunikasi dan mengunci PTT jika sedang sibuk (Busy State)."""
        if self.is_ptt_active:
            return {"success": False, "message": "Jalur komunikasi sibuk (Busy). PTT sedang digunakan user lain."}
        
        # Kunci jalur PTT
        self.is_ptt_active = True
        return {"success": True, "message": "PTT diizinkan. Anda sedang mengudara."}

    def release_ptt(self, user_id, username, channel_id, channel_name):
        """Melepaskan status PTT dan memicu penyimpanan log audio ke database."""
        self.is_ptt_active = False
        
        # Simpan log transmisi radio menggunakan DatabaseManager
        db = DatabaseManager()
        db.save_radio_log(user_id, username, channel_id, channel_name, audio_path="uploads/audio_sample.wav")
        
        return {"success": True, "message": "PTT dilepaskan, log berhasil disimpan."}

    def toggle_global_mute(self, mute_state):
        """Mengatur status mute global (mematikan seluruh output suara aplikasi)."""
        self.is_global_muted = mute_state
        return {"success": True, "global_muted": self.is_global_muted}

    def set_channel_audio_state(self, channel_id, enabled):
        """Mengaktifkan atau menonaktifkan suara masuk secara spesifik pada channel tertentu."""
        self.channel_audio_states[channel_id] = enabled
        return {"success": True, "channel_id": channel_id, "audio_enabled": enabled}