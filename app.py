"""Loopback-only browser app. No external services or persistent photo storage."""
import argparse
import atexit
import io
import json
import os
import secrets
import shutil
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server
from werkzeug.utils import secure_filename

from inspector import InspectionError, LABELS, clean, inspect

ROOT = Path(__file__).resolve().parent
MAX_FILE = 64 * 1024 * 1024
MAX_SESSION = 2 * 1024 * 1024 * 1024


def create_app(input_dir=ROOT):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_FILE + 1024 * 1024
    folder = Path(tempfile.mkdtemp(prefix='photo-metadata-'))
    token = secrets.token_urlsafe(32)
    items = {}
    lock = threading.RLock()
    app.extensions['photos'] = items
    app.extensions['photo_temp'] = folder
    app.extensions['photo_token'] = token
    app.extensions['photo_lock'] = lock
    def dispose():
        shutil.rmtree(folder, ignore_errors=True)
    app.extensions['dispose'] = dispose
    atexit.register(dispose)

    @app.before_request
    def local_only():
        if request.host.split(':')[0] not in ('127.0.0.1', 'localhost'):
            abort(403, 'Only local access is allowed.')
        if request.method != 'GET' and not secrets.compare_digest(request.headers.get('X-Photo-Token', ''), token):
            abort(403, 'Reload this app before continuing.')

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, InspectionError):
            return jsonify(error=str(exc)), 422
        app.logger.exception('Local processing error')
        return jsonify(error='Processing failed. See the app terminal for details.'), 500

    def public(item):
        return {k: v for k, v in item.items() if k not in ('path', 'clean_path')}

    def get_item(photo_id):
        if photo_id not in items:
            abort(404, 'Photo not found in this session.')
        return items[photo_id]

    def import_photo(stream, name):
        name = name.replace('\\', '/').rsplit('/', 1)[-1]
        if Path(name).suffix.lower() not in ('.jpg', '.jpeg'):
            raise InspectionError('Only JPG and JPEG exports are supported.')
        if sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) >= MAX_SESSION:
            raise InspectionError('Session storage is full (2 GB). Restart the app for a fresh session.')
        photo_id = uuid.uuid4().hex
        path = folder / (photo_id + '.jpg')
        size = 0
        try:
            with path.open('xb') as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise InspectionError('Each JPEG must be 64 MB or smaller.')
                    output.write(chunk)
            try:
                report = inspect(path)
            except InspectionError as exc:
                report = dict(status='error', label=LABELS['error'], error=str(exc), evidence=[])
            item = dict(id=photo_id, name=name, path=path, report=report, cleanup=None)
            items[photo_id] = item
            return public(item)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @app.get('/')
    def index():
        return render_template('index.html', token=token)

    @app.post('/api/photos')
    def upload():
        if 'photo' not in request.files:
            abort(400, 'Choose a JPEG first.')
        photo = request.files['photo']
        with lock:
            return jsonify(import_photo(photo.stream, photo.filename or 'photo.jpg')), 201

    @app.post('/api/samples')
    def samples():
        results, errors = [], []
        with lock:
            for path in sorted(Path(input_dir).iterdir()):
                if path.is_file() and path.suffix.lower() in ('.jpg', '.jpeg'):
                    try:
                        with path.open('rb') as stream:
                            results.append(import_photo(stream, path.name))
                    except (InspectionError, OSError) as exc:
                        errors.append(dict(name=path.name, error=str(exc)))
        return jsonify(photos=results, errors=errors)

    @app.get('/api/photos')
    def list_photos():
        with lock:
            return jsonify(photos=[public(item) for item in items.values()])

    @app.get('/api/photos/<photo_id>')
    def detail(photo_id):
        with lock:
            return jsonify(public(get_item(photo_id)))

    @app.get('/api/photos/<photo_id>/preview')
    def preview(photo_id):
        item = get_item(photo_id)
        if item['report']['status'] == 'error':
            abort(422, 'Invalid JPEG cannot be previewed.')
        return send_file(item['path'], mimetype='image/jpeg')

    @app.post('/api/clean')
    def clean_batch():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            abort(400, 'Expected a JSON object with selected photo IDs.')
        ids = payload.get('ids')
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            abort(400, 'Select between 1 and 100 photos.')
        results = []
        with lock:
            for photo_id in dict.fromkeys(ids):
                item = items.get(photo_id)
                if item is None:
                    results.append(dict(id=photo_id, status='error', error='Photo not found.'))
                    continue
                if item['report']['status'] == 'error':
                    results.append(dict(id=photo_id, status='error', error='Inspect a valid JPEG first.'))
                    continue
                if not item['report']['evidence']:
                    results.append(dict(id=photo_id, status='skipped', message='No explicit AI indicators to remove.'))
                    continue
                try:
                    if not item['cleanup']:
                        target = folder / (photo_id + '-cleaned.jpg')
                        outcome = clean(item['path'], target, item['report'])
                        item['cleanup'] = outcome
                        item['clean_path'] = target
                    results.append(dict(id=photo_id, status='cleaned', photo=public(item)))
                except (InspectionError, OSError) as exc:
                    results.append(dict(id=photo_id, status='error', error=str(exc)))
        return jsonify(results=results)

    def download_name(item):
        return (secure_filename(Path(item['name']).stem) or 'photo') + '-cleaned.jpg'

    @app.get('/api/photos/<photo_id>/download')
    def download(photo_id):
        item = get_item(photo_id)
        if not item.get('clean_path'):
            abort(404, 'Create a verified cleaned copy first.')
        return send_file(item['clean_path'], mimetype='image/jpeg', as_attachment=True,
                         download_name=download_name(item))

    @app.post('/api/download.zip')
    def batch_download():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            abort(400, 'Expected a JSON object with selected photo IDs.')
        ids = payload.get('ids')
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            abort(400, 'Select between 1 and 100 cleaned photos.')
        archive_path = folder / (uuid.uuid4().hex + '.zip')
        names = set()
        try:
            with lock, zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_STORED) as archive:
                for photo_id in dict.fromkeys(ids):
                    item = get_item(photo_id)
                    if not item.get('clean_path'):
                        abort(409, 'Every selected photo must have a verified cleaned copy.')
                    name = download_name(item)
                    stem = Path(name).stem
                    counter = 2
                    while name in names:
                        name = '%s-%d.jpg' % (stem, counter)
                        counter += 1
                    names.add(name)
                    archive.write(item['clean_path'], name)
            response = send_file(archive_path, mimetype='application/zip', as_attachment=True,
                                 download_name='cleaned-photos.zip')
            # macOS/POSIX retain the open download handle after unlinking the path.
            # Windows retains it until session shutdown instead.
            if os.name == 'posix':
                archive_path.unlink()
            else:
                response.call_on_close(lambda: archive_path.unlink(missing_ok=True))
            return response
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    @app.get('/api/report')
    def report():
        with lock:
            content = dict(created_at=datetime.now(timezone.utc).isoformat(),
                           notes=['Embedded declarations only; signatures not verified.',
                                  'Absence of metadata does not prove absence of AI editing.',
                                  'Cleanup cannot guarantee social-media labeling behavior.'],
                           photos=[public(item) for item in items.values()])
            data = json.dumps(content, indent=2, ensure_ascii=False).encode('utf-8')
        return send_file(io.BytesIO(data), mimetype='application/json', as_attachment=True,
                         download_name='photo-metadata-report.json')

    return app


def main():
    parser = argparse.ArgumentParser(description='Local Photo Metadata Inspector')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--input-dir', type=Path, default=ROOT)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        parser.error('--input-dir must be an existing directory')
    app = create_app(args.input_dir)
    try:
        server = make_server('127.0.0.1', args.port, app, threaded=True)
        url = 'http://127.0.0.1:%d' % server.server_port
        print('Photo Metadata Inspector: ' + url, flush=True)
        print('Photos stay on this Mac. Press Control-C to quit and remove temporary copies.', flush=True)
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.extensions['dispose']()


if __name__ == '__main__':
    main()
