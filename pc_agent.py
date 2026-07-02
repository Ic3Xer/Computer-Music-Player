import os
import sys
import time
import ctypes
import urllib.request
import urllib.parse
import socket
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Windows Master Volume integration using pycaw
has_pycaw = False
try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    has_pycaw = True
    print("[VOLUME] pycaw Core Audio API loaded successfully!")
except ImportError:
    print("[WARNING] pycaw/comtypes not installed. Volume slider will fallback to mock values.")

# Voicemeeter DLL Discovery
voicemeeter_dll_path = None
try:
    import winreg
    # Try 64-bit registry first
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\VB:Voicemeeter {17359A74-1236-5C73}")
        dir_path, _ = winreg.QueryValueEx(key, "UninstallString")
        winreg.CloseKey(key)
        dir_path = os.path.dirname(dir_path)
        dll_path = os.path.join(dir_path, "VoicemeeterRemote64.dll")
        if os.path.exists(dll_path):
            voicemeeter_dll_path = dll_path
    except Exception:
        pass

    if not voicemeeter_dll_path:
        # Try 32-bit registry
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\VB:Voicemeeter {17359A74-1236-5C73}")
            dir_path, _ = winreg.QueryValueEx(key, "UninstallString")
            winreg.CloseKey(key)
            dir_path = os.path.dirname(dir_path)
            dll_path = os.path.join(dir_path, "VoicemeeterRemote64.dll")
            if os.path.exists(dll_path):
                voicemeeter_dll_path = dll_path
        except Exception:
            pass

    if not voicemeeter_dll_path:
        # Fallback default paths
        default_paths = [
            r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll",
            r"C:\Program Files\VB\Voicemeeter\VoicemeeterRemote64.dll",
            r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote.dll"
        ]
        for path in default_paths:
            if os.path.exists(path):
                voicemeeter_dll_path = path
                break

    if voicemeeter_dll_path:
        print(f"[VOICEMEETER] Voicemeeter Remote API DLL found at: {voicemeeter_dll_path}")
    else:
        print("[VOICEMEETER] Voicemeeter DLL not found. Volume control will use system fallback.")
except Exception as e:
    print(f"[VOICEMEETER] Init error: {e}")

# Voicemeeter Threading and DLL instances
voicemeeter_lock = threading.Lock()
vm_dll = None

def init_voicemeeter():
    global vm_dll
    if voicemeeter_dll_path:
        try:
            # Loaded dynamically but kept open
            vm_dll = ctypes.windll.LoadLibrary(voicemeeter_dll_path)
            res_login = vm_dll.VBVMR_Login()
            if res_login == 0 or res_login == 1:
                print("[VOICEMEETER] Logged into Voicemeeter Remote API successfully!")
                
                # Configure parameter prototypes
                vm_dll.VBVMR_GetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)]
                vm_dll.VBVMR_GetParameterFloat.restype = ctypes.c_long
                vm_dll.VBVMR_SetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.c_float]
                vm_dll.VBVMR_SetParameterFloat.restype = ctypes.c_long
            else:
                print(f"[VOICEMEETER] VBVMR_Login failed with code: {res_login}")
                vm_dll = None
        except Exception as e:
            print(f"[VOICEMEETER] Failed to load/login: {e}")
            vm_dll = None

def close_voicemeeter():
    global vm_dll
    if vm_dll:
        try:
            vm_dll.VBVMR_Logout()
            print("[VOICEMEETER] Logged out from Voicemeeter Remote API.")
        except Exception:
            pass
        vm_dll = None

# Windows Virtual Key Codes
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_BROWSER_HOME = 0xAC

# Global variables
passcode_hash = "51cfb4dcc56c05e1b5f5e8dd49e9e723978a083b6ed03853eaf284673e82e03c"  # icetine
start_time = time.time()
script_dir = os.path.dirname(os.path.realpath(__file__))
index_html_path = os.path.join(script_dir, "index.html")
player_html_path = os.path.join(script_dir, "player.html")

# Music Queue State
music_queue = []  # Elements: {"id": video_id, "title": title}
current_song = None  # Current: {"id": video_id, "title": title} or None
direct_play_process = None
last_player_poll_time = 0.0

def extract_youtube_id(text):
    import re
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:music\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None

def verify_and_get_title(video_id):
    import urllib.request
    import json
    import re
    # Use oembed to get the title and check basic existence
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    req = urllib.request.Request(oembed_url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            title = data.get("title", "YouTube Video")
            
            # Verify if it's embeddable on the main page
            url = f"https://www.youtube.com/watch?v={video_id}"
            req_main = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            with urllib.request.urlopen(req_main, timeout=5) as resp_main:
                html = resp_main.read().decode('utf-8')
                embeddable = '"playableInEmbed":true' in html or '"isEmbeddable":true' in html
                playable = '"playabilityStatus":{"status":"UNPLAYABLE"' not in html
                
                # Try to extract duration
                duration_seconds = 240 # Fallback 4 minutes
                duration_match = re.search(r'"approxDurationMs":"(\d+)"', html)
                if duration_match:
                    duration_seconds = int(duration_match.group(1)) // 1000
                
                if not playable:
                    return False, title, False, 0, "วิดีโอนี้ไม่สามารถเล่นได้ (อาจจะติดลิขสิทธิ์หรือจำกัดอายุ)"
                if not embeddable:
                    return True, title, False, duration_seconds, "ผู้ลงวิดีโอบล็อกการเล่นนอก YouTube (Embedding disabled)"
                return True, title, True, duration_seconds, "OK"
    except Exception as e:
        return False, "Unknown Video", False, 0, f"เกิดข้อผิดพลาดในการตรวจสอบวิดีโอ: {e}"

def search_youtube(query):
    import urllib.request
    import urllib.parse
    import re
    
    # 1. Check if query is already a direct YouTube link
    direct_id = extract_youtube_id(query)
    if direct_id:
        playable, title, embeddable, duration, reason = verify_and_get_title(direct_id)
        if playable:
            return direct_id, title, embeddable, duration, "OK"
        else:
            return None, title, False, 0, reason

    # 2. Search query and test the top results
    search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    req = urllib.request.Request(search_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode('utf-8')
            video_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            
            # Deduplicate video IDs
            seen = set()
            unique_ids = [x for x in video_ids if not (x in seen or seen.add(x))]
            
            playable_candidates = []
            # Test up to top 5 results
            for vid_id in unique_ids[:5]:
                playable, title, embeddable, duration, reason = verify_and_get_title(vid_id)
                if playable:
                    if embeddable:
                        print(f"[SEARCH SUCCESS] Found embeddable video: {title} ({vid_id})")
                        return vid_id, title, True, duration, "OK"
                    else:
                        playable_candidates.append((vid_id, title, False, duration))
                        print(f"[SEARCH PLAYABLE] Skipping {vid_id} ({title}) - Can only play direct (not embeddable)")
                else:
                    print(f"[SEARCH SKIP] Skipping {vid_id} ({title}) - Reason: {reason}")
            
            # Fallback to the first playable (direct-only) video if no embeddable one is found
            if playable_candidates:
                vid_id, title, embeddable, duration = playable_candidates[0]
                print(f"[SEARCH FALLBACK] Using direct-play video: {title} ({vid_id})")
                return vid_id, title, False, duration, "OK"
                    
    except Exception as e:
        print(f"[YT SEARCH ERROR] {e}")
        
    return None, None, False, 0, "ไม่พบผลลัพธ์ที่เล่นได้บนเว็บ"

def close_direct_youtube():
    global current_song
    import ctypes
    
    title_to_find = None
    if current_song and current_song.get("title"):
        title_to_find = current_song["title"]
        # Clean title to get core part (first 25 characters)
        if len(title_to_find) > 25:
            title_to_find = title_to_find[:25]
            
    WM_CLOSE = 0x0010
    
    def enum_handler(hwnd, lParam):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value
            
            match = False
            if "YouTube" in title:
                if title_to_find:
                    if title_to_find.lower() in title.lower():
                        match = True
                else:
                    if "Smart Room" not in title and ("Chrome" in title or "Edge" in title):
                        match = True
                        
            if match:
                print(f"[WIN32] Closing direct YouTube window: '{title}' (HWND: {hwnd})")
                ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True
        
    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    EnumWindows(EnumWindowsProc(enum_handler), 0)

def close_player_browser():
    import ctypes
    WM_CLOSE = 0x0010
    
    def enum_handler(hwnd, lParam):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buff = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value
            
            if "YT Queue Player" in title or "Smart Room 52 Jukebox" in title:
                print(f"[WIN32] Closing player window: '{title}' (HWND: {hwnd})")
                ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True
        
    EnumWindows = ctypes.windll.user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    EnumWindows(EnumWindowsProc(enum_handler), 0)

def open_direct_youtube(url):
    global direct_play_process
    close_direct_youtube() # Close previous one first
    
    import subprocess
    import os
    
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
    ]
    
    launched = False
    for path in chrome_paths:
        if os.path.exists(path):
            try:
                # Open in a new window using the default profile to preserve YT Premium login
                direct_play_process = subprocess.Popen([
                    path,
                    "--new-window",
                    url
                ])
                launched = True
                print(f"[BROWSER] Opened YouTube in a new Chrome window (PID: {direct_play_process.pid})")
                break
            except Exception as e:
                print(f"[BROWSER ERROR] Chrome launch failed: {e}")
                
    if not launched:
        # Edge fallback
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        ]
        for path in edge_paths:
            if os.path.exists(path):
                try:
                    direct_play_process = subprocess.Popen([
                        path,
                        "--new-window",
                        url
                    ])
                    launched = True
                    print(f"[BROWSER] Opened YouTube in a new Edge window (PID: {direct_play_process.pid})")
                    break
                except Exception as e:
                    print(f"[BROWSER ERROR] Edge launch failed: {e}")
                    
    if not launched:
        import webbrowser
        webbrowser.open(url)
        print("[BROWSER] Fallback standard webbrowser.open executed")

def open_player_browser():
    import subprocess
    import os
    url = "http://127.0.0.1:8000/player"
    
    # Try Chrome with autoplay bypass in a new window using the default profile
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
    ]
    chrome_opened = False
    for path in chrome_paths:
        if os.path.exists(path):
            try:
                subprocess.Popen([path, "--new-window", "--autoplay-policy=no-user-gesture-required", url])
                chrome_opened = True
                print("[BROWSER] Launched Chrome player window with autoplay bypass")
                break
            except Exception:
                pass
                
    if not chrome_opened:
        # Try Edge with autoplay bypass in a new window
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        ]
        for path in edge_paths:
            if os.path.exists(path):
                try:
                    subprocess.Popen([path, "--new-window", "--autoplay-policy=no-user-gesture-required", url])
                    chrome_opened = True
                    print("[BROWSER] Launched Edge player window with autoplay bypass")
                    break
                except Exception:
                    pass
                    
    if not chrome_opened:
        import webbrowser
        webbrowser.open(url)
        print("[BROWSER] Opened player in default browser (fallback)")

def get_voicemeeter_strip_index(vm_dll):
    try:
        vtype = ctypes.c_long()
        if vm_dll.VBVMR_GetVoicemeeterType(ctypes.byref(vtype)) == 0:
            vm_type = vtype.value
            if vm_type == 1:
                return 2  # Voicemeeter Standard (Virtual Input 1)
            elif vm_type == 2:
                return 3  # Voicemeeter Banana (Virtual Input 1)
            elif vm_type == 3:
                return 5  # Voicemeeter Potato (Virtual Input 1)
    except Exception:
        pass
    return 5  # Fallback to Potato index (Voicemeeter Input)

def get_pc_volume():
    """Returns the volume of Voicemeeter Input, or fallback to Windows master volume."""
    global vm_dll
    if vm_dll:
        with voicemeeter_lock:
            try:
                strip_idx = get_voicemeeter_strip_index(vm_dll)
                gain = ctypes.c_float()
                param_name = f"Strip[{strip_idx}].Gain".encode('ascii')
                res_get = vm_dll.VBVMR_GetParameterFloat(param_name, ctypes.byref(gain))
                if res_get == 0:
                    # Map gain (-60.0 to 0.0 dB) to percentage (0 to 100)
                    gain_val = gain.value
                    vol_percent = int(round((gain_val + 60.0) / 60.0 * 100.0))
                    return max(0, min(100, vol_percent))
            except Exception as e:
                print(f"[VOICEMEETER ERROR] Failed to get volume parameter: {e}")

    # Fallback to Windows master volume
    if not has_pycaw:
        return 50
    try:
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        speakers = AudioUtilities.GetSpeakers()
        volume = speakers.EndpointVolume
        current_volume = volume.GetMasterVolumeLevelScalar()
        return int(round(current_volume * 100))
    except Exception as e:
        print(f"[ERROR] Fallback failed to get volume: {e}")
        return 50

def set_pc_volume(volume_percent):
    """Sets the volume of Voicemeeter Input, or fallback to Windows master volume."""
    global vm_dll
    vol = max(0, min(100, int(volume_percent)))
    
    if vm_dll:
        with voicemeeter_lock:
            try:
                strip_idx = get_voicemeeter_strip_index(vm_dll)
                # Map percentage (0 to 100) to gain (-60.0 to 0.0 dB)
                gain_val = -60.0 + (vol / 100.0) * 60.0
                param_name = f"Strip[{strip_idx}].Gain".encode('ascii')
                res_set = vm_dll.VBVMR_SetParameterFloat(param_name, ctypes.c_float(gain_val))
                if res_set == 0:
                    print(f"[VOICEMEETER] Set Strip[{strip_idx}].Gain to {gain_val:.2f} dB ({vol}%)")
                    return
            except Exception as e:
                print(f"[VOICEMEETER ERROR] Failed to set volume parameter: {e}")

    # Fallback to Windows master volume
    if not has_pycaw:
        print("[WARNING] pycaw not available. Fallback volume cannot be set.")
        return
    try:
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        speakers = AudioUtilities.GetSpeakers()
        volume = speakers.EndpointVolume
        volume.SetMasterVolumeLevelScalar(vol / 100.0, None)
        print(f"[VOLUME] Set master volume to {vol}% (Voicemeeter fallback)")
    except Exception as e:
        print(f"[ERROR] Fallback failed to set volume: {e}")

def update_hub_status(vol):
    """Sends volume state updates to the Main Hub (ESP32 at 192.168.0.103)."""
    try:
        url = (
            f"http://192.168.0.103/api/device/update"
            f"?id=pc_controller"
            f"&value={vol}"
            f"&name=PC%20Controller"
            f"&type=media"
            f"&port=8000"
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'PC-Agent'})
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
    except Exception:
        pass

def register_with_hub_async(vol):
    """Sends dynamic volume state updates to the Main Hub in a background thread."""
    threading.Thread(target=update_hub_status, args=(vol,), daemon=True).start()

def press_key(vk_code):
    """Simulates a key press and release of a virtual key code."""
    try:
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)  # KeyDown
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)  # KeyUp (0x0002)
        print(f"[KEY] Simulated keypress for VK code: {hex(vk_code)}")
    except Exception as e:
        print(f"[ERROR] Failed to simulate keypress: {e}")

class PCAgentHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress logging to prevent console clutter
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, token')
        self.end_headers()

    def do_GET(self):
        global current_song, music_queue, last_player_poll_time
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/":
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            try:
                with open(index_html_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))
            except Exception:
                self.wfile.write(b"<h1>PC Media Controller</h1><p>Error loading index.html</p>")
                
        elif path == "/player":
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            try:
                with open(player_html_path, "r", encoding="utf-8") as f:
                    self.wfile.write(f.read().encode("utf-8"))
            except Exception:
                self.wfile.write(b"<h1>YT Queue Player</h1><p>Error loading player.html</p>")
                
        elif path == "/api/status":
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            uptime_seconds = int(time.time() - start_time)
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            seconds = uptime_seconds % 60
            uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            
            status = {
                "wifi": -30,
                "uptime": uptime_str,
                "pc_online": True,
                "volume": get_pc_volume()
            }
            self.wfile.write(json.dumps(status).encode('utf-8'))

        elif path == "/api/queue/status":
            last_player_poll_time = time.time()
            # If current song is direct-play only and has not been launched yet, open it now on PC
            if current_song and not current_song.get("embeddable", True) and not current_song.get("opened_direct", False):
                current_song["opened_direct"] = True
                current_song["start_time"] = time.time()
                open_direct_youtube(f"https://www.youtube.com/watch?v={current_song['id']}")
                print(f"[DIRECT PLAY] Automatically opened non-embeddable song on PC: {current_song['title']} (Duration: {current_song.get('duration', 240)}s)")

            # If the current song is direct-play and duration has elapsed, auto-advance
            if current_song and not current_song.get("embeddable", True) and current_song.get("opened_direct", False):
                elapsed = time.time() - current_song.get("start_time", time.time())
                if elapsed > current_song.get("duration", 240):
                    print(f"[QUEUE AUTO-ADVANCE] Direct-play song '{current_song['title']}' finished. Skipping to next.")
                    close_direct_youtube()
                    if music_queue:
                        current_song = music_queue.pop(0)
                    else:
                        current_song = None

            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "current": current_song,
                "queue": music_queue
            }).encode('utf-8'))

        elif path == "/api/queue/next":
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            ended_id = query.get("ended_id", [""])[0]
            should_advance = True
            if ended_id and current_song:
                if ended_id != current_song.get("id"):
                    should_advance = False
                    print(f"[QUEUE] Blocked duplicate auto-advance request. ended_id '{ended_id}' does not match current '{current_song.get('id')}'")
            
            if should_advance:
                close_direct_youtube()
                if music_queue:
                    current_song = music_queue.pop(0)
                    if current_song:
                        current_song["opened_direct"] = False
                else:
                    current_song = None
                print(f"[QUEUE] Auto-advanced to next song: {current_song['title'] if current_song else 'None'}")
                
            self.wfile.write(json.dumps({
                "status": "ok",
                "current": current_song
            }).encode('utf-8'))
            
        elif path == "/api/queue/fallback":
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if current_song:
                current_song["embeddable"] = False
                current_song["opened_direct"] = False
                print(f"[FALLBACK] Song '{current_song['title']}' failed in embed, switching to direct play on PC.")
            self.wfile.write(json.dumps({
                "status": "ok",
                "current": current_song
            }).encode('utf-8'))
            
        elif path == "/api/media":
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            cmd = query.get("cmd", [""])[0].lower()
            token = query.get("token", [""])[0]

            if token != passcode_hash:
                self.wfile.write(json.dumps({"error": "Unauthorized passcode"}).encode('utf-8'))
                return

            print(f"[API] Network command executed: {cmd.upper()}")
            if cmd == "play":
                press_key(VK_MEDIA_PLAY_PAUSE)
            elif cmd == "next":
                press_key(VK_MEDIA_NEXT_TRACK)
            elif cmd == "prev":
                press_key(VK_MEDIA_PREV_TRACK)
            elif cmd == "volup":
                current_vol = get_pc_volume()
                set_pc_volume(current_vol + 5)
                register_with_hub_async(get_pc_volume())
            elif cmd == "voldown":
                current_vol = get_pc_volume()
                set_pc_volume(max(0, current_vol - 5))
                register_with_hub_async(get_pc_volume())
            elif cmd == "mute":
                press_key(VK_VOLUME_MUTE)
            elif cmd == "volume":
                val = query.get("val", [""])[0]
                if val.isdigit():
                    set_pc_volume(int(val))
                    register_with_hub_async(int(val))
                else:
                    self.wfile.write(json.dumps({"error": "Invalid volume value"}).encode('utf-8'))
                    return
            elif cmd == "ytmusic":
                song = query.get("song", [""])[0]
                if song:
                    # Search/validate song (both direct links and text queries)
                    video_id, title, embeddable, duration, reason = search_youtube(song)
                    if video_id:
                        new_item = {
                            "id": video_id,
                            "title": title,
                            "embeddable": embeddable,
                            "duration": duration,
                            "opened_direct": False
                        }
                        music_queue.append(new_item)
                        print(f"[QUEUE] Added: {title} ({video_id}) [Embeddable: {embeddable}, Duration: {duration}s]")
                        
                        # If nothing is playing, play immediately and auto-open player on PC
                        if current_song is None:
                            current_song = music_queue.pop(0)
                            if time.time() - last_player_poll_time > 2.0:
                                open_player_browser()
                            else:
                                print("[QUEUE] Player is already active/polling. Skip opening new window.")
                            
                        self.wfile.write(json.dumps({"status": "ok", "added": new_item}).encode('utf-8'))
                        return
                    else:
                        self.wfile.write(json.dumps({"error": reason}).encode('utf-8'))
                        return
                else:
                    self.wfile.write(json.dumps({"error": "Missing song name"}).encode('utf-8'))
                    return
            elif cmd == "skip":
                close_direct_youtube()
                if music_queue:
                    current_song = music_queue.pop(0)
                else:
                    current_song = None
                print("[QUEUE] Skipped song")
            elif cmd == "clear":
                close_direct_youtube()
                close_player_browser()
                music_queue = []
                current_song = None
                print("[QUEUE] Cleared queue")
            else:
                self.wfile.write(json.dumps({"error": "Invalid command"}).encode('utf-8'))
                return

            self.wfile.write(json.dumps({"status": "ok", "sent_command": cmd}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

def start_http_server():
    try:
        server = HTTPServer(('0.0.0.0', 8000), PCAgentHTTPHandler)
        print("🌐 [PC API] HTTP API server listening on port 8000 (http://0.0.0.0:8000)")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[ERROR] Failed to start HTTP API server: {e}")

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def hub_heartbeat_loop():
    while True:
        current_vol = get_pc_volume()
        update_hub_status(current_vol)
        time.sleep(20)

def main():
    import atexit
    init_voicemeeter()
    atexit.register(close_direct_youtube)
    atexit.register(close_player_browser)
    atexit.register(close_voicemeeter)
    print("====================================================")
    print("      Smart Room 52 - PC Control Background Agent    ")
    print("====================================================")
    print("Operating System: Windows")
    print("Press Ctrl+C to exit.")
    
    # Start heartbeat registry thread
    heartbeat_thread = threading.Thread(target=hub_heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    
    local_ip = get_local_ip()
    print(f"✨ [DIRECT CONTROL] เปิดเว็บคุมเครื่องนี้ตรงๆ ได้ที่: http://{local_ip}:8000")
    
    try:
        start_http_server()
    except KeyboardInterrupt:
        pass
    print("\n[EXIT] Exiting...")

if __name__ == "__main__":
    main()
