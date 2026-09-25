from flask import Flask, Response, request, jsonify
import yt_dlp
import requests
import time
import threading

from flask_cors import CORS
app = Flask(__name__)
CORS(app)

# URL cache: video_id -> (audio_url, timestamp)
_url_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600 * 4  # 4 hours


def get_cached_audio_url(video_id):
    with _cache_lock:
        if video_id in _url_cache:
            url, ts = _url_cache[video_id]
            if time.time() - ts < _CACHE_TTL:
                return url
            del _url_cache[video_id]
    return None


def cache_audio_url(video_id, url):
    with _cache_lock:
        _url_cache[video_id] = (url, time.time())


def extract_media_url(video_id, media_type="audio"):
    cached = get_cached_audio_url(f"{video_id}_{media_type}")
    if cached:
        return cached

    yt_url = f"https://www.youtube.com/watch?v={video_id}"
    strategies = [
        {'format': 'best[ext=mp4]/best' if media_type == 'video' else 'bestaudio[ext=m4a]/bestaudio/best', 'noplaylist': True, 'quiet': True, 'no_warnings': True,
         'extractor_args': {'youtube': {'player_client': ['mediaconnect']}}},
        {'format': 'best[ext=mp4]/best' if media_type == 'video' else 'bestaudio[ext=m4a]/bestaudio/best', 'noplaylist': True, 'quiet': True, 'no_warnings': True,
         'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}},
        {'format': 'best[ext=mp4]/best' if media_type == 'video' else 'bestaudio[ext=m4a]/bestaudio/best', 'noplaylist': True, 'quiet': True, 'no_warnings': True,
         'extractor_args': {'youtube': {'player_client': ['web_music']}}},
    ]

    for opts in strategies:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(yt_url, download=False)
                audio_url = info.get('url')
                if audio_url:
                    cache_audio_url(video_id, audio_url)
                    return audio_url
        except Exception:
            continue

    return None


@app.route('/')
def home():
    return jsonify({"status": "running", "message": "Media Vault Proxy API is online!"})


@app.route('/api/search')
def search_videos():
    query = request.args.get('q')
    if not query:
        return jsonify({"error": "No query provided"}), 400

    opts = {
        'extract_flat': True,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch20:{query}", download=False)
            results = []
            for entry in info.get('entries', []):
                if entry.get('id'):
                    thumb = ''
                    thumbs = entry.get('thumbnails')
                    if thumbs and len(thumbs) > 0:
                        thumb = thumbs[0].get('url', '')

                    duration_val = entry.get('duration')
                    if duration_val is None:
                        duration_val = 0

                    results.append({
                        'id': entry['id'],
                        'title': entry.get('title', 'Unknown'),
                        'author': entry.get('uploader', 'Unknown'),
                        'durationMs': int(duration_val) * 1000,
                        'thumbnail': thumb,
                        'url': entry.get('url', f"https://www.youtube.com/watch?v={entry['id']}")
                    })
            return jsonify({"status": "success", "results": results})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/extract')
def extract_audio():
    video_id = request.args.get('v')
    if not video_id:
        return jsonify({"error": "No video id provided"}), 400

    audio_url = extract_media_url(video_id, "audio")
    if audio_url:
        return jsonify({"status": "success", "direct_url": audio_url})
    return jsonify({"status": "error", "error": "All strategies failed"}), 500


@app.route('/api/stream')
def stream_audio():
    """Proxy stream - extracts audio URL then streams bytes through this server.
    This ensures the phone never connects to googlevideo.com directly."""
    video_id = request.args.get('id')
    if not video_id:
        return jsonify({"error": "No video id provided"}), 400

    audio_url = extract_media_url(video_id, "audio")
    if not audio_url:
        return jsonify({"error": "Could not extract audio URL"}), 500

    # Build headers for the upstream request
    upstream_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    # Forward Range header for seeking support
    if request.headers.get('Range'):
        upstream_headers['Range'] = request.headers.get('Range')

    try:
        r = requests.get(audio_url, stream=True, headers=upstream_headers, timeout=30)

        response_headers = {
            'Content-Type': r.headers.get('Content-Type', 'audio/mp4'),
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
        }
        if r.headers.get('Content-Length'):
            response_headers['Content-Length'] = r.headers.get('Content-Length')
        if r.headers.get('Content-Range'):
            response_headers['Content-Range'] = r.headers.get('Content-Range')

        return Response(
            r.iter_content(chunk_size=8192),
            status=r.status_code,
            headers=response_headers
        )
    except Exception as e:
        # Cached URL might be stale - clear and retry once
        with _cache_lock:
            if video_id in _url_cache:
                del _url_cache[video_id]

        audio_url = extract_media_url(video_id, "audio")
        if not audio_url:
            return jsonify({"error": f"Stream failed: {str(e)}"}), 500

        try:
            r = requests.get(audio_url, stream=True, headers=upstream_headers, timeout=30)
            response_headers = {
                'Content-Type': r.headers.get('Content-Type', 'audio/mp4'),
                'Accept-Ranges': 'bytes',
                'Access-Control-Allow-Origin': '*',
            }
            if r.headers.get('Content-Length'):
                response_headers['Content-Length'] = r.headers.get('Content-Length')
            if r.headers.get('Content-Range'):
                response_headers['Content-Range'] = r.headers.get('Content-Range')

            return Response(
                r.iter_content(chunk_size=8192),
                status=r.status_code,
                headers=response_headers
            )
        except Exception as e2:
            return jsonify({"error": f"Stream retry failed: {str(e2)}"}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000, threaded=True)
