import hashlib
import io
import json
import os
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import DISK_RESERVE, MAX_FILE, ROOT, create_app
from inspector import InspectionError, classify, clean, inspect, preserved_parts, segments, tool

SAMPLES = ['25EE0779-Edit.jpg', '398A7418.jpg', '398A7475.jpg']
HASHES = ['942141adc57c892ba3bb74ae2fa71749139bea8f579db12d7cfac2e6382f515d',
          'e117e8d7ae875620cac8536b202dd39ebf5c3b9f5af92e866bd0c14eb80a141a',
          '5387594d608ea3cd49c7da622f48931d52cc0db24fce3885a7e7025424a9e6bf']
EDITED = 'http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia'
GENERATED = 'http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia'
requires_samples = unittest.skipUnless(
    all((ROOT / name).is_file() for name in SAMPLES),
    'Private sample JPEGs are not bundled; see README.md for local integration tests.')


def box(kind, payload):
    return struct.pack('>I', len(payload) + 8) + kind + payload


def non_ai_credential(data):
    description = bytes.fromhex('6332706100110010800000aa00389b71') + b'\x03c2pa\0'
    jumbf = box(b'jumb', box(b'jumd', description) + box(b'json', b'{"creator":"Photographer","action":"color correction"}'))
    payload = b'JP\x00\x01\x00\x00\x00\x01' + jumbf
    return data[:2] + b'\xff\xeb' + struct.pack('>H', len(payload) + 2) + payload + data[2:]


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def copy(self, name='test.jpg', source=SAMPLES[0]):
        path = self.folder / name
        path.write_bytes((ROOT / source).read_bytes())
        return path

    @requires_samples
    def test_supplied_findings_and_original_hashes(self):
        for name, digest, expected in zip(SAMPLES, HASHES, ['none', 'none', 'edited']):
            path = ROOT / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            report = inspect(path)
            self.assertEqual(report['status'], expected)
        report = inspect(ROOT / SAMPLES[2])
        self.assertEqual(report['software'][0]['name'], 'Adobe Lightroom Classic')
        self.assertTrue(any(e['value'] == 'Healing changed, Uses GenAI' for e in report['evidence']))

    @requires_samples
    def test_real_cleanup_preserves_every_other_byte(self):
        source = ROOT / SAMPLES[2]
        target = self.folder / 'cleaned.jpg'
        original = source.read_bytes()
        outcome = clean(source, target)
        self.assertFalse(outcome['report']['credential_present'])
        self.assertEqual(outcome['report']['status'], 'none')
        self.assertEqual(outcome['bytes_removed'], 14448)
        self.assertEqual(target.read_bytes(), original[:2] + original[14450:])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), HASHES[2])

    @requires_samples
    def test_xmp_generation_and_editing_removal_preserves_copyright(self):
        for index, uri in enumerate((EDITED, GENERATED)):
            with self.subTest(uri=uri):
                source = self.copy('xmp%d.jpg' % index)
                tool('-overwrite_original', '-XMP-iptcExt:DigitalSourceType=' + uri,
                     '-XMP-dc:Rights=Copyright Example Photographer', '-Artist=Example', source)
                before = inspect(source)
                self.assertEqual(before['status'], 'edited' if uri == EDITED else 'generated')
                output = self.folder / ('out%d.jpg' % index)
                result = clean(source, output, before)
                self.assertEqual(result['report']['status'], 'none')
                self.assertIn('Copyright Example Photographer', json.dumps(result['report']['metadata']))
                self.assertIn('Example', json.dumps(result['report']['metadata']))

    @requires_samples
    def test_credential_without_ai_is_not_removed(self):
        source = self.copy()
        source.write_bytes(non_ai_credential(source.read_bytes()))
        report = inspect(source)
        self.assertEqual(report['status'], 'credentials')
        self.assertEqual(report['evidence'], [])
        self.assertIsNone(clean(source, self.folder / 'unused.jpg', report))
        self.assertFalse((self.folder / 'unused.jpg').exists())

    @requires_samples
    def test_combined_credential_and_xmp_cleanup(self):
        source = self.copy(source=SAMPLES[2])
        tool('-overwrite_original', '-XMP-iptcExt:DigitalSourceType=' + EDITED, source)
        report = inspect(source)
        self.assertEqual({e['source'] for e in report['evidence']}, {'metadata', 'C2PA'})
        result = clean(source, self.folder / 'combined-clean.jpg', report)
        self.assertEqual(result['report']['status'], 'none')

    @requires_samples
    def test_unrelated_app11_cannot_be_excluded_from_preservation(self):
        data = (ROOT / SAMPLES[2]).read_bytes()
        payload = b'JP\x00\x02\x00\x00\x00\x01unrelated metadata'
        unrelated = b'\xff\xeb' + struct.pack('>H', len(payload) + 2) + payload
        with_extra = data[:2] + unrelated + data[2:]
        self.assertIn(unrelated, preserved_parts(with_extra, remove_jumbf=True))

    def test_multiple_scans_stuffing_and_restart_markers(self):
        def segment(marker, payload):
            return b'\xff' + bytes([marker]) + struct.pack('>H', len(payload) + 2) + payload
        scan1 = b'\x11\xff\x00\x22\xff\xd0\x33'
        scan2 = b'\x55\xff\x00\x66'
        data = b'\xff\xd8' + segment(0xc2, b'frame') + segment(0xda, b'first') + scan1 + segment(0xc4, b'table') + segment(0xda, b'second') + scan2 + b'\xff\xd9trailer'
        parts = segments(data)
        self.assertEqual([raw for marker, raw, _ in parts if marker == -2], [scan1, scan2])
        self.assertEqual(b''.join(raw for _, raw, _ in parts), data)

    def test_ordinary_adobe_software_and_synthetic_composite_are_not_ai(self):
        for value in ['Adobe Photoshop', 'http://cv.iptc.org/newscodes/digitalsourcetype/compositeSynthetic']:
            status, evidence = classify({'XMP-xmp:CreatorTool': value}, {}, False)
            self.assertEqual(status, 'none')
            self.assertEqual(evidence, [])

    @requires_samples
    def test_malformed_and_truncated_jpegs(self):
        for data in (b'hello', b'\xff\xd8\xff\xd9', (ROOT / SAMPLES[0]).read_bytes()[:-2]):
            with self.subTest(length=len(data)), self.assertRaises(InspectionError):
                segments(data)

    @requires_samples
    def test_output_and_source_guards(self):
        source = self.copy(source=SAMPLES[2])
        before = inspect(source)
        with self.assertRaises(InspectionError):
            clean(source, source, before)
        existing = self.copy('existing.jpg')
        with self.assertRaises(InspectionError):
            clean(source, existing, before)
        source.write_bytes(source.read_bytes() + b'changed')
        with self.assertRaisesRegex(InspectionError, 'changed after inspection'):
            clean(source, self.folder / 'new.jpg', before)

    @requires_samples
    def test_corrupt_cleanup_is_rejected_and_deleted(self):
        import inspector
        original_tool = inspector.tool
        def damaging_tool(*args):
            output = original_tool(*args)
            if '-overwrite_original' in args:
                path = Path(args[-1])
                path.write_bytes(path.read_bytes() + b'unrelated damage')
            return output
        target = self.folder / 'damaged.jpg'
        with patch('inspector.tool', side_effect=damaging_tool):
            with self.assertRaisesRegex(InspectionError, 'Preservation check failed'):
                clean(ROOT / SAMPLES[2], target)
        self.assertFalse(target.exists())

    @requires_samples
    def test_unhandled_declaration_refuses_cleanup(self):
        report = inspect(ROOT / SAMPLES[0])
        report['evidence'] = [dict(source='metadata', field='IPTC:DigitalSourceType', value=EDITED)]
        with self.assertRaisesRegex(InspectionError, 'cannot be removed safely'):
            clean(ROOT / SAMPLES[0], self.folder / 'out.jpg', report)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.testing = True
        self.client = self.app.test_client()
        self.headers = {'X-Photo-Token': self.app.extensions['photo_token']}

    def tearDown(self):
        self.app.extensions['dispose']()

    def upload(self, name, data, relative_path=None):
        fields = {'photo': (io.BytesIO(data), name)}
        if relative_path is not None:
            fields['relative_path'] = relative_path
        response = self.client.post('/api/photos', headers=self.headers,
                                    data=fields)
        response.request.environ['wsgi.input'].close()
        return response

    def test_folder_paths_are_kept_without_becoming_storage_paths(self):
        first = self.upload('same.JPG', b'bad jpeg', 'Wedding/Ceremony/same.JPG').get_json()
        second = self.upload('same.JPG', b'bad jpeg', 'Wedding/Reception/same.JPG').get_json()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['name'], second['name'])
        self.assertEqual(first['relative_path'], 'Wedding/Ceremony/same.JPG')
        self.assertEqual(second['relative_path'], 'Wedding/Reception/same.JPG')
        self.assertEqual(self.upload('same.JPG', b'bad jpeg', '../same.JPG').status_code, 422)
        self.assertEqual(len(list(self.app.extensions['photo_temp'].iterdir())), 2)
        with self.client.get('/api/report') as response:
            self.assertEqual(json.loads(response.data)['photos'], [first, second])

    def test_configured_folder_recurses_and_continues_past_bad_files(self):
        with tempfile.TemporaryDirectory() as input_dir:
            root = Path(input_dir)
            (root / 'Ceremony').mkdir()
            (root / 'Reception' / 'Dancing').mkdir(parents=True)
            for name in ('Ceremony/same.JPG', 'Reception/Dancing/same.jpeg', 'skip.png', '._extra.jpg'):
                (root / name).write_bytes(b'not a jpeg')
            (root / 'link').symlink_to(root, target_is_directory=True)
            with (root / 'Ceremony' / 'oversize.jpg').open('wb') as f:
                f.truncate(MAX_FILE + 1)
            app = create_app(root)
            self.addCleanup(app.extensions['dispose'])
            response = app.test_client().post('/api/samples',
                                              headers={'X-Photo-Token': app.extensions['photo_token']})
        result = response.get_json()
        self.assertEqual([p['relative_path'] for p in result['photos']],
                         ['Ceremony/same.JPG', 'Reception/Dancing/same.jpeg'])
        self.assertEqual(result['errors'][0]['name'], 'Ceremony/oversize.jpg')

    def test_disk_space_replaces_fixed_session_quota(self):
        folder = self.app.extensions['photo_temp']
        with (folder / 'existing-copy.jpg').open('wb') as f:
            f.truncate(2 * 1024 ** 3)
        with patch('app.shutil.disk_usage', return_value=SimpleNamespace(free=8 * 1024 ** 3)):
            first = self.upload('photo.jpg', b'bad jpeg')
        self.assertEqual(first.status_code, 201)
        previous = set(folder.iterdir())
        with patch('app.shutil.disk_usage', return_value=SimpleNamespace(free=DISK_RESERVE)):
            full = self.upload('next.jpg', b'bad jpeg')
        self.assertEqual(full.status_code, 507)
        self.assertIn('disk space', full.get_json()['error'])
        self.assertEqual(set(folder.iterdir()), previous)

    @requires_samples
    def test_cleaning_updates_only_selected_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'Wedding' / 'Ceremony' / 'same.jpg'
            second = root / 'Wedding' / 'Reception' / 'same.jpg'
            malformed = root / 'Wedding' / 'bad.jpg'
            photo_data = (ROOT / SAMPLES[2]).read_bytes()
            for path in (first, second):
                path.parent.mkdir(parents=True)
                path.write_bytes(photo_data)
                path.chmod(0o640)
            os.utime(first, (1_700_000_000, 1_700_000_000))
            if sys.platform == 'darwin':
                subprocess.run(['/usr/bin/xattr', '-w', 'com.example.photo-test', 'wedding', str(first)], check=True)
            malformed.write_bytes(b'not a jpeg')
            with patch('app.choose_originals', return_value=[root / 'Wedding']) as chooser:
                response = self.client.post('/api/choose', headers=self.headers, json={'kind': 'folders'})
            chooser.assert_called_once_with('folders')
            self.assertEqual(response.status_code, 200)
            photos = {p['relative_path']: p for p in response.get_json()['photos']}
            selected = photos['Wedding/Ceremony/same.jpg']
            broken = photos['Wedding/bad.jpg']
            self.assertTrue(selected['editable'])
            self.assertEqual(broken['report']['status'], 'error')
            self.assertFalse(list(self.app.extensions['photo_temp'].iterdir()))
            self.assertEqual(first.read_bytes(), photo_data)
            response = self.client.post('/api/clean', headers=self.headers,
                                        json={'ids': [selected['id'], broken['id'], 'missing']})
            results = response.get_json()['results']
            self.assertEqual([result['status'] for result in results], ['cleaned', 'error', 'error'])
            expected = photo_data[:2] + photo_data[14450:]
            self.assertEqual(first.read_bytes(), expected)
            self.assertEqual(second.read_bytes(), photo_data)
            self.assertEqual(first.stat().st_mode & 0o777, 0o640)
            self.assertEqual(first.stat().st_mtime, 1_700_000_000)
            if sys.platform == 'darwin':
                self.assertEqual(subprocess.check_output(
                    ['/usr/bin/xattr', '-p', 'com.example.photo-test', str(first)]).strip(), b'wedding')
            self.assertEqual(list(first.parent.iterdir()), [first])
            with self.client.get('/api/photos/' + selected['id'] + '/preview') as preview:
                self.assertEqual(preview.data, expected)
            retry = self.client.post('/api/clean', headers=self.headers, json={'ids': [selected['id']]})
            self.assertEqual(retry.get_json()['results'][0]['status'], 'cleaned')
            with self.client.get('/api/report') as report:
                entry = next(p for p in json.loads(report.data)['photos'] if p['id'] == selected['id'])
                self.assertEqual(entry['cleanup']['report']['status'], 'none')
                self.assertNotIn('path', entry)
                self.assertNotIn('state', entry)
                self.assertNotIn(str(root), report.data.decode())

    @requires_samples
    def test_changed_or_failed_original_is_left_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'photo.jpg'
            shutil.copyfile(ROOT / SAMPLES[2], path)
            with patch('app.choose_originals', return_value=[path]):
                response = self.client.post('/api/choose', headers=self.headers, json={'kind': 'files'})
            photo_id = response.get_json()['photos'][0]['id']
            photo_data = path.read_bytes()
            with patch('inspector.os.replace', side_effect=PermissionError('Cannot replace photo')):
                failed = self.client.post('/api/clean', headers=self.headers, json={'ids': [photo_id]})
            self.assertEqual(failed.get_json()['results'][0]['status'], 'error')
            self.assertEqual(path.read_bytes(), photo_data)
            self.assertEqual(list(path.parent.iterdir()), [path])
            path.write_bytes(photo_data + b'changed')
            changed = self.client.post('/api/clean', headers=self.headers, json={'ids': [photo_id]})
            self.assertIn('changed after inspection', changed.get_json()['results'][0]['error'])
            self.assertEqual(path.read_bytes(), photo_data + b'changed')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_cancelled_picker_changes_nothing(self):
        with patch('app.choose_originals', return_value=[]):
            response = self.client.post('/api/choose', headers=self.headers, json={'kind': 'folders'})
        self.assertEqual(response.get_json(), dict(photos=[], errors=[], cancelled=True))

    def test_local_request_guards_and_invalid_inputs(self):
        self.assertEqual(self.client.post('/api/samples').status_code, 403)
        self.assertEqual(self.client.get('/', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.upload('fake.png', b'abc').status_code, 422)
        self.assertEqual(self.client.get('/api/photos/missing').status_code, 404)
        self.assertEqual(self.client.post('/api/clean', headers=self.headers, json={'ids': []}).status_code, 400)
        self.assertEqual(self.client.post('/api/clean', headers=self.headers, json={'ids': [42]}).status_code, 400)
        for route in ['/api/clean', '/api/choose']:
            self.assertEqual(self.client.post(route, headers=self.headers, json=['invalid']).status_code, 400)
        self.assertEqual(self.client.post('/api/choose', json={'kind': 'files'}).status_code, 403)
        response = self.client.get('/')
        self.assertIn(b'Photo Metadata Inspector', response.data)
        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])

    @requires_samples
    def test_uploaded_photo_is_inspection_only(self):
        photo = self.upload('photo.jpg', (ROOT / SAMPLES[2]).read_bytes()).get_json()
        self.assertFalse(photo['editable'])
        response = self.client.post('/api/clean', headers=self.headers, json={'ids': [photo['id']]})
        result = response.get_json()['results'][0]
        self.assertEqual(result['status'], 'error')
        self.assertIn('Choose the original', result['error'])

    @requires_samples
    def test_session_cleanup(self):
        folder = self.app.extensions['photo_temp']
        self.upload('plain.jpg', (ROOT / SAMPLES[0]).read_bytes())
        self.assertTrue(any(folder.iterdir()))
        self.app.extensions['dispose']()
        self.assertFalse(folder.exists())


if __name__ == '__main__':
    unittest.main()
