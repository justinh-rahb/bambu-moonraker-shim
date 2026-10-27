"""Read-only slicer metadata extraction. No printer commands or live-job fallbacks."""
import base64
import hashlib
import io
import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from PIL import Image, UnidentifiedImageError

PARSER_VERSION = 3
MAX_FILE_SIZE = 256 * 1024 * 1024
MAX_MEMBER_SIZE = 128 * 1024 * 1024
MAX_CONFIG_SIZE = 4 * 1024 * 1024
MAX_IMAGE_SIZE = 8 * 1024 * 1024


class MetadataError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def validate_filename(filename):
    if not isinstance(filename, str) or not filename:
        raise MetadataError(400, 'Filename required')
    if filename.startswith('gcodes/'):
        filename = filename[7:]
    if any(c in filename for c in ('\\', '\r', '\n', '\0')) or any(p in ('', '.', '..') for p in filename.split('/')):
        raise MetadataError(400, 'Invalid filename')
    if not filename.lower().endswith(('.gcode', '.3mf')):
        raise MetadataError(400, 'Metadata supports .gcode and .3mf files')
    return filename


def numbers(value):
    values = []
    for token in re.split(r'[,;]', str(value)):
        try:
            number = float(token.strip().strip('"'))
            if math.isfinite(number) and number >= 0:
                values.append(number)
        except ValueError:
            pass
    return values


def duration(value):
    parts = re.findall(r'(\d+(?:\.\d+)?)\s*([dhms])', value)
    if parts:
        return round(sum(float(n) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit] for n, unit in parts))
    values = numbers(value)
    return round(values[0]) if values else None


def image_info(data):
    """Inspect only supported raster headers; never execute/extract archive paths."""
    if len(data) > MAX_IMAGE_SIZE:
        return None
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33 and data[12:16] == b'IHDR':
        width, height = struct.unpack('>II', data[16:24])
        kind = 'png'
    elif data.startswith(b'\xff\xd8'):
        offset = 2
        width = height = 0
        while offset + 4 <= len(data):
            if data[offset] != 255:
                break
            marker = data[offset + 1]
            offset += 2
            if marker in (0xD8, 0xD9):
                continue
            length = int.from_bytes(data[offset:offset + 2], 'big')
            if length < 2 or offset + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2) and length >= 7:
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                break
            offset += length
        kind = 'jpg'
    else:
        return None
    if not (0 < width <= 8192 and 0 < height <= 8192):
        return None
    return width, height, kind


def parse_gcode(data):
    text = data.decode('utf-8-sig', errors='replace')
    result = {}
    images = []
    config = {}
    for line in text.splitlines():
        match = re.match(r'^;\s*([^=\r\n]+?)\s*=\s*(.*)$', line)
        if match:
            config[match[1].strip().lower()] = match[2].strip()
    generated = re.search(r';\s*generated (?:by|with)\s+(OrcaSlicer|BambuStudio|Bambu Studio|PrusaSlicer|SuperSlicer)\s+([^\s]+)', text, re.I)
    if not generated:
        generated = re.search(r'^;\s*(BambuStudio|OrcaSlicer)\s+([\d][^\s]*)', text, re.M)
    if generated:
        result.update(slicer=generated[1].replace('Bambu Studio', 'BambuStudio'), slicer_version=generated[2])
    else:
        cura = re.search(r';Generated with Cura_SteamEngine\s+([^\s]+)', text)
        if cura:
            result.update(slicer='Cura', slicer_version=cura[1])

    aliases = {
        'layer_height': ('layer_height',),
        'first_layer_height': ('initial_layer_print_height', 'first_layer_height'),
        'nozzle_diameter': ('nozzle_diameter',),
        'first_layer_extr_temp': ('nozzle_temperature_initial_layer', 'first_layer_temperature'),
        'first_layer_bed_temp': ('bed_temperature_initial_layer_single', 'first_layer_bed_temperature'),
        'chamber_temp': ('chamber_temperature',),
    }
    used_indexes = [i for i, length in enumerate(numbers(config.get('filament used [mm]', ''))) if length > 0]
    for output, keys in aliases.items():
        for key in keys:
            values = numbers(config.get(key, ''))
            if values:
                if len(set(values)) == 1:
                    result[output] = values[0]
                elif output in ('first_layer_extr_temp', 'first_layer_bed_temp', 'chamber_temp') and len(used_indexes) == 1 and used_indexes[0] < len(values):
                    result[output] = values[used_indexes[0]]
                break
    patterns = {
        'object_height': r'^;\s*(?:max_z_height|MAXZ)\s*[:=]\s*([\d.]+)',
        'layer_count': r'^;\s*(?:total layer number|LAYER_COUNT)\s*[:=]\s*(\d+)',
        'layer_height': r'^;\s*Layer height:\s*([\d.]+)',
    }
    for output, pattern in patterns.items():
        match = re.search(pattern, text, re.M | re.I)
        if match:
            result[output] = int(match[1]) if output == 'layer_count' else float(match[1])
    if 'object_height' not in result:
        heights = re.findall(r'^;\s*(?:Z_HEIGHT|Z)\s*:\s*([\d.]+)\s*$', text, re.M)
        if heights:
            result['object_height'] = max(map(float, heights))
    time_match = re.search(r'total estimated time:\s*([^;\r\n]+)', text)
    if not time_match:
        time_match = re.search(r'^;\s*estimated printing time(?: \(normal mode\))?\s*=\s*([^\r\n]+)', text, re.M)
    if not time_match:
        time_match = re.search(r'^;TIME:\s*(\d+)', text, re.M)
    if time_match:
        value = duration(time_match[1])
        if value is not None:
            result['estimated_time'] = value
    used = numbers(config.get('filament used [mm]', ''))
    if not used:
        # Cura records metres, unlike Prusa/Bambu/Orca's millimetres.
        match = re.search(r'^;Filament used:\s*([\d.]+)m\s*$', text, re.M)
        if match:
            used = [float(match[1]) * 1000]
    weights = numbers(config.get('filament used [g]', ''))
    if used:
        result['filament_total'] = sum(used)
    else:
        total = re.search(r'^;\s*total filament length \[mm\]\s*[:=]\s*([\d.]+)', text, re.M)
        if total:
            result['filament_total'] = float(total[1])
    if weights:
        result.update(filament_weight_total=sum(weights), filament_weights=weights)
    else:
        total = re.search(r'^;\s*total filament weight \[g\]\s*[:=]\s*([\d.]+)', text, re.M)
        if total:
            result['filament_weight_total'] = float(total[1])
    indexes = [i for i, length in enumerate(used) if length > 0]
    for output, key in (('filament_type', 'filament_type'), ('filament_name', 'filament_settings_id')):
        values = [v.strip().strip('"') for v in config.get(key, '').split(';') if v.strip()]
        if values:
            selected = [values[i] for i in indexes if i < len(values)] if indexes else values[:1] if len(set(values)) == 1 else []
            if selected:
                result[output] = ';'.join(dict.fromkeys(selected))
    colors = config.get('filament_colour', config.get('filament_color', ''))
    if colors:
        values = [v.strip().strip('"') for v in re.split('[;,]', colors)]
        selected = [values[i] for i in indexes if i < len(values)] if indexes else values[:1] if len(set(values)) == 1 else []
        selected = [v for v in selected if re.fullmatch(r'#[0-9a-fA-F]{6}', v)]
        if selected:
            result['filament_colors'] = selected
    for match in re.finditer(r'^;\s*thumbnail(?:_JPG|_PNG)? begin[^\n]*\n(.*?)^;\s*thumbnail(?:_JPG|_PNG)? end', text, re.M | re.S | re.I):
        encoded = re.sub(r'[;\s]', '', match[1])
        if len(encoded) > MAX_IMAGE_SIZE * 4 // 3 + 4:
            continue
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        if image_info(image):
            images.append(image)
    return result, images


def parse_file(data, filename, plate=None):
    """Return factual metadata and supported preview bytes for the selected plate."""
    if len(data) > MAX_FILE_SIZE:
        raise MetadataError(413, 'File exceeds the metadata parser limit (256 MiB)')
    if not filename.lower().endswith('.3mf'):
        if plate not in (None, 1):
            raise MetadataError(400, 'Plain G-code has no selectable plates')
        return parse_gcode(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 10000:
                raise MetadataError(413, 'Too many archive members')
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise MetadataError(422, 'Archive contains duplicate members')
            plates = sorted(int(m[1]) for name in names if (m := re.fullmatch(r'Metadata/plate_(\d+)\.gcode', name)))
            if plate is None:
                plate = 1 if 1 in plates else plates[0] if plates else None
            if plate not in plates:
                raise MetadataError(422, f'No sliced G-code for plate {plate}')

            def read(name, limit):
                info = archive.getinfo(name)
                if info.file_size > limit or info.flag_bits & 1:
                    raise MetadataError(413, f'Unsupported or oversized archive member: {name}')
                with archive.open(info) as stream:
                    value = stream.read(limit + 1)
                if len(value) > limit:
                    raise MetadataError(413, f'Oversized archive member: {name}')
                return value

            result, images = parse_gcode(read(f'Metadata/plate_{plate}.gcode', MAX_MEMBER_SIZE))
            result.update(plate=plate, plate_count=len(plates))
            if 'Metadata/slice_info.config' in names:
                xml = read('Metadata/slice_info.config', MAX_CONFIG_SIZE)
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                    raise MetadataError(422, 'Unsupported XML declaration')
                tree = ET.fromstring(xml)
                for element in tree.findall('plate'):
                    info = {m.get('key'): m.get('value') for m in element.findall('metadata')}
                    if str(info.get('index')) != str(plate):
                        continue
                    for output, key in (('estimated_time', 'prediction'), ('filament_weight_total', 'weight'), ('nozzle_diameter', 'nozzle_diameters'), ('first_layer_time', 'first_layer_time')):
                        values = numbers(info.get(key, ''))
                        if values:
                            result.setdefault(output, values[0])
                    filaments = [f.attrib for f in element.findall('filament') if any(v > 0 for v in numbers(f.get('used_m', '')) + numbers(f.get('used_g', '')))]
                    if filaments:
                        lengths = [sum(numbers(f.get('used_m', ''))) * 1000 for f in filaments]
                        weights = [sum(numbers(f.get('used_g', ''))) for f in filaments]
                        if any(lengths):
                            result.setdefault('filament_total', sum(lengths))
                        if any(weights):
                            result.setdefault('filament_weight_total', sum(weights))
                            result.setdefault('filament_weights', weights)
                        for output, key in (('filament_type', 'type'), ('filament_colors', 'color')):
                            values = [f[key] for f in filaments if f.get(key)]
                            if values:
                                result[output] = values if output.endswith('colors') else ';'.join(dict.fromkeys(values))
            previews = [f'Metadata/plate_{plate}_small.png', f'Metadata/plate_{plate}.png']
            for name in previews:
                if name in names:
                    image = read(name, MAX_IMAGE_SIZE)
                    if image_info(image):
                        images.append(image)
            return result, images
    except MetadataError:
        raise
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, RuntimeError, EOFError) as exc:
        raise MetadataError(422, f'Cannot parse sliced 3MF: {exc}') from exc


def thumbnail_records(images):
    records, blobs = [], {}
    largest = None
    largest_area = 0
    for image in images:
        info = image_info(image)
        if not info:
            continue
        width, height, kind = info
        if width * height > 16 * 1024 * 1024:
            continue
        try:
            with Image.open(io.BytesIO(image)) as preview:
                preview.load()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            continue
        if width * height > largest_area:
            largest, largest_area = image, width * height
        key = hashlib.sha256(image).hexdigest() + '.' + kind
        if key in blobs:
            continue
        records.append(dict(width=width, height=height, size=len(image), relative_path='.thumbs/' + key))
        blobs[key] = image
    # Bambu's small preview is commonly 128px; Mainsail's list requires 30–64px.
    if largest and not any(30 <= t['width'] <= 64 and 30 <= t['height'] <= 64 for t in records):
        with Image.open(io.BytesIO(largest)) as preview:
            preview = preview.convert('RGBA')
            preview.thumbnail((64, 64), Image.Resampling.LANCZOS)
            canvas = Image.new('RGBA', (64, 64))
            canvas.paste(preview, ((64 - preview.width) // 2, (64 - preview.height) // 2))
            stream = io.BytesIO()
            canvas.save(stream, format='PNG')
            image = stream.getvalue()
        key = hashlib.sha256(image).hexdigest() + '.png'
        records.append(dict(width=64, height=64, size=len(image), relative_path='.thumbs/' + key))
        blobs[key] = image
    return sorted(records, key=lambda v: (v['width'] * v['height'], v['size'])), blobs
