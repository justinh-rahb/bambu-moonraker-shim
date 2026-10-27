"""Async file metadata access with persistent fingerprinted caches and single-flight reads."""
import asyncio
import hashlib
import os
import posixpath
import time
import weakref
import uuid

from .config import Config
from .file_metadata import (MetadataError, MAX_FILE_SIZE, PARSER_VERSION, parse_file,
                            thumbnail_records, validate_filename)
from .ftps_client import DownloadLimitError


class MetadataService:
    def __init__(self):
        self._locks = weakref.WeakValueDictionary()
        self._workers = asyncio.Semaphore(2)

    @staticmethod
    def _source(filename, ftp, db, fresh=False):
        if not Config.BAMBU_SERIAL:
            root = os.path.realpath(Config.GCODES_DIR)
            path = os.path.realpath(os.path.join(root, filename))
            if os.path.commonpath([root, path]) != root:
                raise MetadataError(400, 'File escapes gcodes root')
            if not os.path.isfile(path):
                raise MetadataError(404, 'File not found')
            stat = os.stat(path)
            return path, stat.st_size, stat.st_mtime
        parent, name = posixpath.split(filename)
        entries = None if fresh or parent else db.get_cached_files(max_age=300)
        if entries is None:
            entries = ftp.list_files(posixpath.join(Config.BAMBU_FTPS_UPLOADS_DIR, parent))
            if not parent:
                db.clear_file_cache()
                db.cache_files(entries)
        entry = next((e for e in entries if not e.get('is_dir') and e.get('name') == name), None)
        if entry is None:
            raise MetadataError(404, 'File not found')
        return filename, int(entry['size']), float(entry.get('modified') or 0)

    async def get(self, filename, plate, force, ftp, db):
        filename = validate_filename(filename)
        if plate is not None and (isinstance(plate, bool) or not isinstance(plate, int) or plate < 1):
            raise MetadataError(400, 'Plate must be a positive integer')
        cache_plate = plate or 0
        key = (filename, plate)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock, self._workers:
            try:
                source, size, modified = await asyncio.to_thread(self._source, filename, ftp, db, force)
                if size > MAX_FILE_SIZE:
                    raise MetadataError(413, 'File exceeds metadata parser limit (256 MiB)')
                cached = await asyncio.to_thread(db.get_parsed_metadata, filename, cache_plate, PARSER_VERSION, size, modified)
                # Without a printer timestamp, periodically re-read to catch same-size changes.
                if not force and cached and (modified or time.time() - cached.get('metadata_extracted', 0) < 300):
                    return cached
                if Config.BAMBU_SERIAL:
                    data = await asyncio.to_thread(ftp.download_file, source, max_size=MAX_FILE_SIZE)
                else:
                    def read():
                        with open(source, 'rb') as stream:
                            return stream.read(MAX_FILE_SIZE + 1)
                    data = await asyncio.to_thread(read)
                if len(data) != size:
                    if Config.BAMBU_SERIAL:
                        await asyncio.to_thread(db.clear_file_cache)
                    raise MetadataError(409, 'File size changed during metadata read; refresh and retry')
                # A full fallback FTPS listing can require one SIZE request per file.
                # With no modification timestamp it cannot detect same-size changes;
                # the bounded download and periodic/forced scans handle that case.
                if not Config.BAMBU_SERIAL or modified:
                    _, final_size, final_modified = await asyncio.to_thread(self._source, filename, ftp, db, True)
                    if (final_size, final_modified) != (size, modified):
                        raise MetadataError(409, 'File changed during metadata read; refresh and retry')
                fields, images = await asyncio.to_thread(parse_file, data, filename, plate)
                thumbnails, blobs = await asyncio.to_thread(thumbnail_records, images)
                result = dict(fields, filename=filename, size=size, modified=modified,
                              thumbnails=thumbnails, metadata_extracted=time.time(),
                              uuid=str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(data).hexdigest() + f':{fields.get("plate", 1)}:{PARSER_VERSION}')))
                await asyncio.to_thread(db.cache_parsed_metadata, filename, cache_plate, PARSER_VERSION, result, blobs)
                return result
            except MetadataError:
                raise
            except FileNotFoundError as exc:
                raise MetadataError(404, 'File not found') from exc
            except DownloadLimitError as exc:
                raise MetadataError(413, str(exc)) from exc
            except Exception as exc:
                raise MetadataError(502, f'Metadata read failed: {exc}') from exc


metadata_service = MetadataService()
