"""Loopback-only browser app. No external services or persistent photo storage."""
import argparse
import atexit
import errno
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server
from inspector import InspectionError, LABELS, clean_in_place, file_state, inspect

ROOT = Path(__file__).resolve().parent
MAX_FILE = 64 * 1024 * 1024
DISK_RESERVE = 256 * 1024 * 1024
DISK_ERROR = 'The Mac is low on temporary disk space. Free up space and try again.'
JPEG_SUFFIXES = ('.jpg', '.jpeg')


class StorageError(OSError):
    pass


def out_of_space(exc):
    return isinstance(exc, StorageError) or getattr(exc, 'errno', None) in (errno.ENOSPC, errno.EDQUOT)


def choose_originals(kind):
    if sys.platform != 'darwin':
        raise InspectionError('The native picker requires macOS. Use the configured folder to open originals.')
    chooser = {'files': 'chooseFile', 'folders': 'chooseFolder'}[kind]
    script = f'''
const app = Application.currentApplication();
app.includeStandardAdditions = true;
const selected = app.{chooser}({{withPrompt: 'Choose original photo {kind}', multipleSelectionsAllowed: true}});
JSON.stringify((Array.isArray(selected) ? selected : [selected]).map(String));
'''
    try:
        result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script],
                                capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise InspectionError('The Mac picker timed out. Choose your originals again.') from exc
    if result.returncode:
        if '(-128)' in result.stderr:
            return []
        raise InspectionError('The Mac picker could not open. You can also launch with --input-dir.')
    return [Path(path) for path in json.loads(result.stdout)]


def create_app(input_dir=ROOT):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = MAX_FILE + 1024 * 1024
    folder = Path(tempfile.mkdtemp(prefix='photo-metadata-')).resolve()
    input_root = Path(input_dir).resolve()
    excluded_folders = {folder, ROOT / '.git', ROOT / '.venv'}
    token = secrets.token_urlsafe(32)
    items = {}
    original_ids = {}
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
        if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(request.headers.get('X-Photo-Token', ''), token):
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
        if out_of_space(exc):
            return jsonify(error=DISK_ERROR), 507
        if isinstance(exc, InspectionError):
            return jsonify(error=str(exc)), 422
        app.logger.exception('Local processing error')
        return jsonify(error='Processing failed. See the app terminal for details.'), 500

    def public(item):
        return {k: v for k, v in item.items() if k not in ('path', 'state')}

    def get_item(photo_id):
        if photo_id not in items:
            abort(404, 'Photo not found in this session.')
        return items[photo_id]

    def require_space(size, location=folder):
        if shutil.disk_usage(location).free < size + DISK_RESERVE:
            raise StorageError(DISK_ERROR)

    def inspect_report(path):
        try:
            return inspect(path)
        except InspectionError as exc:
            return dict(status='error', label=LABELS['error'], error=str(exc), evidence=[])

    def import_photo(stream, name, relative_path=None):
        name = name.replace('\\', '/').rsplit('/', 1)[-1]
        if Path(name).suffix.lower() not in JPEG_SUFFIXES:
            raise InspectionError('Only JPG and JPEG exports are supported.')
        if relative_path is not None:
            parts = relative_path.split('/')
            if (parts[-1] != name or '\\' in relative_path or '\0' in relative_path
                    or any(part in ('', '.', '..') for part in parts)):
                raise InspectionError('Invalid relative photo path.')
        stream.seek(0, io.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        if size > MAX_FILE:
            raise InspectionError('Each JPEG must be 64 MiB or smaller.')
        require_space(size)
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
                        raise InspectionError('Each JPEG must be 64 MiB or smaller.')
                    output.write(chunk)
            item = dict(id=photo_id, name=name, path=path, report=inspect_report(path),
                        cleanup=None, editable=False)
            if relative_path is not None:
                item['relative_path'] = relative_path
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
            return jsonify(import_photo(photo.stream, photo.filename or 'photo.jpg',
                                        request.form.get('relative_path'))), 201

    def inspect_originals(paths, folder_selection, include_root=False):
        results, errors = {}, []

        def register(path, relative):
            if path.name.startswith('._') or path.suffix.lower() not in JPEG_SUFFIXES:
                return
            try:
                if path.is_symlink():
                    return
                path = path.resolve()
                state = file_state(path.lstat())
                if state[2] > MAX_FILE:
                    raise InspectionError('Each JPEG must be 64 MiB or smaller.')
                photo_id = original_ids.get(path) or uuid.uuid4().hex
                item = items.get(photo_id)
                if item is None or item['state'] != state:
                    report = inspect_report(path)
                    if file_state(path.lstat()) != state:
                        raise InspectionError('The source changed during inspection. Choose it again.')
                    item = dict(id=photo_id, name=path.name, path=path, state=state, report=report,
                                cleanup=None, editable=True)
                    items[photo_id] = item
                    original_ids[path] = photo_id
                item['relative_path'] = relative
                results[photo_id] = public(item)
            except (InspectionError, OSError) as exc:
                errors.append(dict(name=relative, error=getattr(exc, 'strerror', None) or str(exc)))

        def walk(root):
            prefix = root.name + '/' if include_root and root.name else ''

            def unreadable(exc):
                relative = Path(exc.filename).relative_to(root).as_posix() if exc.filename else '.'
                errors.append(dict(name=prefix + relative, error=exc.strerror or 'Could not read this folder.'))

            for directory, folders, files in os.walk(root, onerror=unreadable):
                directory = Path(directory)
                folders[:] = sorted(name for name in folders if directory / name not in excluded_folders
                                    and not name.startswith('.photo-metadata-'))
                for name in sorted(files):
                    path = directory / name
                    register(path, prefix + path.relative_to(root).as_posix())

        with lock:
            for path in paths:
                if folder_selection:
                    walk(path.resolve())
                else:
                    register(path, path.name)
        return dict(photos=list(results.values()), errors=errors)

    @app.post('/api/samples')
    def samples():
        return jsonify(inspect_originals([input_root], folder_selection=True))

    @app.post('/api/choose')
    def choose():
        payload = request.get_json(silent=True)
        kind = payload.get('kind') if isinstance(payload, dict) else None
        if kind not in ('files', 'folders'):
            abort(400, 'Choose original files or folders.')
        paths = choose_originals(kind)
        if not paths:
            return jsonify(photos=[], errors=[], cancelled=True)
        return jsonify(inspect_originals(paths, folder_selection=kind == 'folders', include_root=True))

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
        with lock:
            item = get_item(photo_id)
            if item['report']['status'] == 'error':
                abort(422, 'Invalid JPEG cannot be previewed.')
            if not item['editable']:
                return send_file(item['path'], mimetype='image/jpeg')
            try:
                original = os.fdopen(os.open(item['path'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
            except OSError:
                abort(409, 'Choose this photo again to refresh its preview.')
            try:
                info = os.fstat(original.fileno())
                if file_state(info) != item['state']:
                    abort(409, 'Choose this photo again to refresh its preview.')
                response = send_file(original, mimetype='image/jpeg', conditional=False)
                response.content_length = info.st_size
                return response
            except Exception:
                original.close()
                raise

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
                if not item['editable']:
                    results.append(dict(id=photo_id, status='error',
                                        error='Choose the original with the Mac picker to clean it in place.'))
                    continue
                if not item['report']['evidence']:
                    results.append(dict(id=photo_id, status='skipped', message='No explicit AI indicators to remove.'))
                    continue
                try:
                    if item['cleanup']:
                        if file_state(item['path'].lstat()) != item['state']:
                            raise InspectionError('The source changed after cleanup. Choose it again to re-inspect it.')
                    else:
                        require_space(2 * item['report']['size'], item['path'].parent)
                        outcome, state = clean_in_place(item['path'], item['report'], item['state'])
                        item['cleanup'] = outcome
                        item['state'] = state
                    results.append(dict(id=photo_id, status='cleaned', photo=public(item)))
                except (InspectionError, OSError) as exc:
                    results.append(dict(id=photo_id, status='error',
                                        error=DISK_ERROR if out_of_space(exc) else getattr(exc, 'strerror', None) or str(exc)))
        return jsonify(results=results)

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
