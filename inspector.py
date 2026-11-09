"""Read metadata with ExifTool and verify JPEG preservation before replacing a file."""
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


LABELS = {
    'edited': 'AI editing recorded',
    'generated': 'AI generation recorded',
    'credentials': 'Credentials present without explicit AI evidence',
    'none': 'No embedded AI indicators found',
    'error': 'Inspection error',
}
AI_TYPES = {'trainedAlgorithmicMedia': 'generated',
            'compositeWithTrainedAlgorithmicMedia': 'edited'}
XMP_PREFIX = b'http://ns.adobe.com/xap/1.0/\0'


class InspectionError(ValueError):
    pass


def tool(*args):
    executable = os.environ.get('EXIFTOOL_PATH') or shutil.which('exiftool')
    if not executable:
        raise InspectionError('ExifTool is missing. Install it with: brew install exiftool')
    try:
        result = subprocess.run([executable, *map(str, args)], capture_output=True,
                                timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError('ExifTool could not finish: ' + str(exc)) from exc
    if result.returncode:
        raise InspectionError(result.stderr.decode('utf-8', 'replace').strip() or
                              'ExifTool could not process this photo.')
    return result.stdout


def segments(data):
    """Split every JPEG marker/scan, including progressive scans and trailing bytes.

    Retaining raw segments lets preservation be verified without decoding pixels.
    """
    if not data.startswith(b'\xff\xd8'):
        raise InspectionError('This file is not a JPEG.')
    result = [(0xd8, data[:2], b'')]
    pos = 2
    saw_frame = saw_scan = False
    while pos < len(data):
        start = pos
        if data[pos] != 255:
            raise InspectionError('Malformed JPEG marker.')
        while pos < len(data) and data[pos] == 255:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xd9:
            result.append((marker, data[start:pos], b''))
            if pos < len(data):
                result.append((-1, data[pos:], data[pos:]))
            if not (saw_frame and saw_scan):
                raise InspectionError('JPEG has no complete image.')
            return result
        if marker in (0, 0xd8) or 0xd0 <= marker <= 0xd7:
            raise InspectionError('Unexpected JPEG marker.')
        if marker == 1:
            result.append((marker, data[start:pos], b''))
            continue
        if pos + 2 > len(data):
            break
        length = int.from_bytes(data[pos:pos+2], 'big')
        if length < 2 or pos + length > len(data):
            raise InspectionError('Truncated JPEG segment.')
        payload = data[pos+2:pos+length]
        pos += length
        result.append((marker, data[start:pos], payload))
        if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                      0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
            saw_frame = True
        if marker == 0xda:
            saw_scan = True
            scan_start = pos
            while pos < len(data):
                if data[pos] != 255:
                    pos += 1
                    continue
                look = pos + 1
                while look < len(data) and data[look] == 255:
                    look += 1
                if look >= len(data):
                    break
                if data[look] == 0 or 0xd0 <= data[look] <= 0xd7:
                    pos = look + 1
                else:
                    break
            result.append((-2, data[scan_start:pos], b''))
    raise InspectionError('JPEG is incomplete: missing end-of-image marker.')


def walk(value, path=''):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + '.' + key if path else key)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk(child, '%s[%d]' % (path, i))
    else:
        yield path, value


def ai_type(value):
    if isinstance(value, str):
        return AI_TYPES.get(value.rstrip('/').rsplit('/', 1)[-1])
    return None


def classify(metadata, credentials, credential_present):
    evidence = []
    for path, value in walk(credentials):
        kind = ai_type(value) if path.lower().endswith('digitalsourcetype') else None
        if not kind and isinstance(value, str) and re.search(r'\bUses GenAI\b', value, re.I):
            kind = 'edited'
        if kind:
            evidence.append(dict(source='C2PA', field=path, value=value, kind=kind))
    for key, value in metadata.items():
        # Only XMP/IPTC declarations outside the signed credential are writable here.
        if key.startswith(('XMP-', 'IPTC:')) and key.endswith(':DigitalSourceType'):
            for path, leaf in walk(value, key):
                kind = ai_type(leaf)
                if kind:
                    evidence.append(dict(source='metadata', field=key, value=leaf, kind=kind))
    kinds = {item['kind'] for item in evidence}
    status = ('generated' if 'generated' in kinds else 'edited' if kinds else
              'credentials' if credential_present else 'none')
    return status, evidence


def inspect(path):
    path = Path(path).resolve()
    data = path.read_bytes()
    parts = segments(data)
    metadata = json.loads(tool('-j', '-G1:3', '-s', '-a', '-u', '-struct', path))[0]
    credentials = json.loads(tool('-j', '-G3', '-s', '-a', '-u', '-struct', '-JUMBF:all', path))[0]
    metadata = {k: v for k, v in metadata.items() if k != 'SourceFile' and not k.startswith('System:')}
    credentials.pop('SourceFile', None)
    problems = [str(v) for k, v in metadata.items() if k.endswith((':Error', ':Warning'))]
    if problems:
        raise InspectionError('Metadata could not be read completely: ' + '; '.join(problems))
    present = bool(credentials) or any(m == 0xeb and p.startswith(b'JP') for m, _, p in parts)
    status, evidence = classify(metadata, credentials, present)
    def tag(suffix):
        return next((v for k, v in metadata.items() if k.endswith(':' + suffix)), None)
    agents = []
    for key, value in credentials.items():
        if key.endswith(':Claim_generator_info'):
            agents.extend(value if isinstance(value, list) else [value])
    return dict(status=status, label=LABELS[status], evidence=evidence,
                credential_present=present, metadata=metadata, credentials=credentials,
                width=tag('ImageWidth'), height=tag('ImageHeight'),
                color_profile=tag('ProfileDescription'), orientation=tag('Orientation'),
                software=agents, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                signature_verification='Not performed; this report describes embedded declarations.')


def preserved_parts(data, remove_jumbf=False, remove_xmp=False):
    parts = segments(data)
    # Remove only APP11 entities whose first packet identifies a C2PA store.
    # Other APP11 data must survive even if ExifTool tries to delete it too.
    c2pa_entities = {p[2:4] for marker, _, p in parts
                     if marker == 0xeb and p.startswith(b'JP')
                     and p[12:16] == b'jumb' and p[20:24] == b'jumd'
                     and p[24:40] == bytes.fromhex('6332706100110010800000aa00389b71')}
    return [raw for marker, raw, payload in parts
            if not (remove_jumbf and marker == 0xeb and payload.startswith(b'JP')
                    and payload[2:4] in c2pa_entities)
            and not (remove_xmp and marker == 0xe1 and payload.startswith(XMP_PREFIX))]


def clean(source, target, before=None):
    """Write a new file only, rescan it, and reject any unintended byte changes."""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve() or target.exists():
        raise InspectionError('Cleanup requires a new output file.')
    before = before or inspect(source)
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != before['sha256']:
        raise InspectionError('The source changed after inspection. Inspect it again.')
    if not before['evidence']:
        return None
    remove_credential = any(e['source'] == 'C2PA' for e in before['evidence'])
    fields = sorted({e['field'] for e in before['evidence'] if e['source'] == 'metadata'})
    for field in fields:
        if not re.fullmatch(r'XMP-[A-Za-z0-9]+:(?:Main:)?DigitalSourceType', field):
            raise InspectionError('This declaration cannot be removed safely yet: ' + field)
    args = ['-overwrite_original']
    if remove_credential:
        args.append('-JUMBF:all=')
    args.extend('-' + field.split(':')[0] + ':DigitalSourceType=' for field in fields)
    if fields:
        toolkit = next((v for k, v in before['metadata'].items() if k.endswith(':XMPToolkit')), '')
        args.append('-XMPToolkit=' + toolkit)
    created = False
    try:
        with target.open('xb') as output:
            created = True
            output.write(original)
        tool(*args, target.resolve())
        after = inspect(target)
        if after['evidence'] or (remove_credential and after['credential_present']):
            raise InspectionError('AI evidence remains after cleanup.')
        cleaned = target.read_bytes()
        if preserved_parts(original, remove_credential, bool(fields)) != preserved_parts(cleaned, remove_credential, bool(fields)):
            raise InspectionError('Preservation check failed: unrelated JPEG bytes changed.')
        # XMP packets may be rewritten, but all non-target XMP values must survive.
        def xmp_values(report):
            return {k: v for k, v in report['metadata'].items()
                    if k.startswith('XMP-') and k not in fields}
        if xmp_values(before) != xmp_values(after):
            raise InspectionError('Preservation check failed: unrelated XMP metadata changed.')
        if hashlib.sha256(source.read_bytes()).hexdigest() != before['sha256']:
            raise InspectionError('Original-file verification failed.')
        return dict(report=after, removed_credential=remove_credential,
                    removed_fields=fields, bytes_removed=len(original)-len(cleaned),
                    verification='Verified: compressed image data and unrelated metadata preserved; original unchanged.')
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def file_state(info):
    if not stat.S_ISREG(info.st_mode):
        raise InspectionError('The source must be a regular JPEG file.')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)


def clean_in_place(source, before, expected_state):
    """Verify an isolated JPEG, then atomically replace the original directory entry."""
    source = Path(source)
    changed = 'The source changed after inspection. Choose it again to re-inspect it.'
    if file_state(source.lstat()) != expected_state:
        raise InspectionError(changed)

    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(source, flags), 'rb') as original:
        info = os.fstat(original.fileno())
        if file_state(info) != expected_state:
            raise InspectionError(changed)
        if info.st_nlink != 1:
            raise InspectionError('Cleanup requires an original without other hard links.')

        with tempfile.TemporaryDirectory(prefix='.photo-metadata-', dir=source.parent) as workspace:
            staged = Path(workspace) / 'verified.jpg'
            outcome = clean(source, staged, before)
            if outcome is None:
                return None, expected_state

            with staged.open('r+b') as replacement:
                if sys.platform == 'darwin':
                    # COPYFILE_METADATA includes stat information, ACLs, and extended attributes.
                    copy_metadata = ctypes.CDLL(None, use_errno=True).fcopyfile
                    copy_metadata.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
                    if copy_metadata(original.fileno(), replacement.fileno(), None, 0b111) != 0:
                        error = ctypes.get_errno()
                        raise OSError(error, os.strerror(error))
                else:
                    shutil.copystat(source, staged)
                copied = os.fstat(replacement.fileno())
                if (copied.st_uid, copied.st_gid, stat.S_IMODE(copied.st_mode)) != (
                        info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)):
                    raise InspectionError('The original file permissions could not be preserved.')
                replacement.seek(0)
                digest = hashlib.sha256()
                for chunk in iter(lambda: replacement.read(1024 * 1024), b''):
                    digest.update(chunk)
                if digest.hexdigest() != outcome['report']['sha256']:
                    raise InspectionError('The staged JPEG changed while preserving file attributes.')
                os.fsync(replacement.fileno())
                if file_state(source.lstat()) != expected_state:
                    raise InspectionError(changed)
                os.replace(staged, source)
                new_state = file_state(os.fstat(replacement.fileno()))

    outcome['verification'] = 'Verified and saved in place. Compressed image data and unrelated metadata were preserved.'
    return outcome, new_state
