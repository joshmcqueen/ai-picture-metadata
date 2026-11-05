import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import ROOT, create_app
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

    def upload(self, name, data):
        response = self.client.post('/api/photos', headers=self.headers,
                                    data={'photo': (io.BytesIO(data), name)})
        response.request.environ['wsgi.input'].close()
        return response

    @requires_samples
    def test_complete_batch_and_downloads_with_duplicate_names_and_error(self):
        photo_data = (ROOT / SAMPLES[2]).read_bytes()
        first = self.upload('same.jpg', photo_data).get_json()
        second = self.upload('same.jpg', photo_data).get_json()
        malformed = self.upload('broken.jpg', b'not a jpeg').get_json()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(malformed['report']['status'], 'error')
        clean_ids = [first['id'], second['id']]
        response = self.client.post('/api/clean', headers=self.headers,
                                    json={'ids': clean_ids + [malformed['id'], 'missing']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['status'] for r in response.get_json()['results']],
                         ['cleaned', 'cleaned', 'error', 'error'])
        individual = self.client.get('/api/photos/' + first['id'] + '/download')
        self.assertEqual(individual.status_code, 200)
        cleaned_data = individual.data
        self.assertEqual(cleaned_data, photo_data[:2] + photo_data[14450:])
        individual.close()
        response = self.client.post('/api/download.zip', headers=self.headers, json={'ids': clean_ids})
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ['same-cleaned.jpg', 'same-cleaned-2.jpg'])
            self.assertEqual(archive.read('same-cleaned.jpg'), cleaned_data)
        response.close()
        report = self.client.get('/api/report')
        body = json.loads(report.data)
        self.assertEqual(len(body['photos']), 3)
        self.assertNotIn('path', body['photos'][0])
        self.assertNotIn(str(self.app.extensions['photo_temp']), report.data.decode())
        report.close()

    def test_local_request_guards_and_invalid_inputs(self):
        self.assertEqual(self.client.post('/api/samples').status_code, 403)
        self.assertEqual(self.client.get('/', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.upload('fake.png', b'abc').status_code, 422)
        self.assertEqual(self.client.get('/api/photos/missing').status_code, 404)
        self.assertEqual(self.client.post('/api/clean', headers=self.headers, json={'ids': []}).status_code, 400)
        self.assertEqual(self.client.post('/api/clean', headers=self.headers, json={'ids': [42]}).status_code, 400)
        for route in ['/api/clean', '/api/download.zip']:
            self.assertEqual(self.client.post(route, headers=self.headers, json=['invalid']).status_code, 400)
        response = self.client.get('/')
        self.assertIn(b'Photo Metadata Inspector', response.data)
        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])

    @requires_samples
    def test_noop_and_unready_download(self):
        photo = self.upload('plain.jpg', (ROOT / SAMPLES[0]).read_bytes()).get_json()
        response = self.client.post('/api/clean', headers=self.headers, json={'ids': [photo['id']]})
        self.assertEqual(response.get_json()['results'][0]['status'], 'skipped')
        self.assertEqual(self.client.get('/api/photos/' + photo['id'] + '/download').status_code, 404)
        self.assertEqual(self.client.post('/api/download.zip', headers=self.headers, json={'ids': [photo['id']]}).status_code, 409)

    @requires_samples
    def test_session_cleanup(self):
        folder = self.app.extensions['photo_temp']
        self.upload('plain.jpg', (ROOT / SAMPLES[0]).read_bytes())
        self.assertTrue(any(folder.iterdir()))
        self.app.extensions['dispose']()
        self.assertFalse(folder.exists())


if __name__ == '__main__':
    unittest.main()
