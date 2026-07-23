import os, json, random, subprocess, tempfile, uuid, io
from flask import Flask, request, jsonify, send_from_directory
import edge_tts
import asyncio
import whisper
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

app = Flask(__name__)

API_SECRET = os.environ["API_SECRET"]
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)
BASE_URL = os.environ.get("BASE_URL", "https://your-service.onrender.com")
VOICE_DEFAULT = "fr-FR-DeniseNeural"

whisper_model = whisper.load_model("base")  # "tiny" si le serveur free tier rame

def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        SERVICE_ACCOUNT_INFO, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)

def pick_random_footage(folder_id):
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType contains 'video/' and trashed=false",
        fields="files(id, name)"
    ).execute()
    files = results.get("files", [])
    if not files:
        raise Exception("Aucune vidéo trouvée dans le dossier Drive.")
    chosen = random.choice(files)
    req = service.files().get_media(fileId=chosen["id"])
    path = os.path.join(tempfile.gettempdir(), f"{chosen['id']}.mp4")
    with io.FileIO(path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return path

async def generate_tts(text, voice, out_path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_path)

def get_audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrapper=1:nokey=1", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())

def transcribe_words(audio_path):
    result = whisper_model.transcribe(audio_path, word_timestamps=True)
    words = []
    for segment in result["segments"]:
        for w in segment.get("words", []):
            words.append({"word": w["word"].strip(), "start": w["start"], "end": w["end"]})
    return words

def build_ass_subtitles(words, ass_path, video_width=1080, video_height=1920):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Neon,Arial Black,90,&H0000FFFF,&H000000FF,&H00FF00FF,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,60,60,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def fmt_time(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:01d}:{m:02d}:{s:05.2f}"

    lines = []
    i = 0
    while i < len(words):
        group = words[i:i+2]  # 2 mots à la fois, effet "pop" façon brainrot
        start, end = group[0]["start"], group[-1]["end"]
        text = " ".join(w["word"] for w in group).upper()
        lines.append(
            f"Dialogue: 0,{fmt_time(start)},{fmt_time(end)},Neon,,0,0,0,,"
            f"{{\\fad(80,80)\\t(0,120,\\fscx120\\fscy120)\\t(120,240,\\fscx100\\fscy100)}}{text}"
        )
        i += 2

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines))

def assemble_video(footage_path, audio_path, ass_path, duration, out_path):
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", footage_path,
        "-i", audio_path,
        "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,ass={ass_path}",
        "-t", str(duration),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest", out_path
    ]
    subprocess.run(cmd, check=True)

@app.route("/generate", methods=["POST"])
def generate():
    if request.headers.get("X-Api-Secret") != API_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json()
    script = data["script"]
    folder_id = data["footageFolderId"]
    voice = data.get("voice", VOICE_DEFAULT)

    job_id = str(uuid.uuid4())
    tmp = tempfile.gettempdir()
    audio_path = os.path.join(tmp, f"{job_id}.mp3")
    ass_path = os.path.join(tmp, f"{job_id}.ass")
    out_filename = f"{job_id}.mp4"
    out_path = os.path.join(OUTPUT_DIR, out_filename)

    try:
        asyncio.run(generate_tts(script, voice, audio_path))
        duration = get_audio_duration(audio_path)
        words = transcribe_words(audio_path)
        build_ass_subtitles(words, ass_path)
        footage_path = pick_random_footage(folder_id)
        assemble_video(footage_path, audio_path, ass_path, duration, out_path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "videoUrl": f"{BASE_URL}/outputs/{out_filename}",
        "durationSeconds": round(duration, 1)
    })

@app.route("/outputs/<path:filename>")
def outputs(filename):
    return send_from_directory(OUTPUT_DIR, filename)

@app.route("/")
def health():
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
