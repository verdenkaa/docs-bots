"""Durable files protected by per-revision OS locks, outside the event loop."""

import json
import os
import shutil
import time
from contextlib import contextmanager
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from rag.contracts import ImageRef, StepError, StorageError
from rag.ingestion.contracts import ConvertedDocument, DocumentBatch, StagedUpload, UploadSource
from rag.runtime._state import _get
from rag.runtime._validation import _staging_revision


def _directory(ref):
    revision = _staging_revision(ref)
    root = _get().settings.spool_dir.resolve()
    uploads = root / "uploads"
    locks = root / ".locks"
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    for directory in (uploads,locks):
        if directory.is_symlink():
            raise StorageError("Spool directory must not be a symlink")
        directory.mkdir(exist_ok=True,mode=0o700)
    target = uploads / str(revision)
    if target.is_symlink() or target.resolve().parent != uploads.resolve():
        raise StorageError("Unsafe spool target")
    return target, locks / (str(revision)+".lock")


@contextmanager
def _guard(ref, *, upload_lease=False):
    target, lock = _directory(ref)
    if upload_lease:
        lock = lock.with_suffix('.upload.lock')
    if lock.is_symlink():
        raise StorageError("Unsafe spool lock")
    with lock.open("a+b") as file:
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        deadline = time.monotonic()+_get().settings.io_timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    file.seek(0)
                    msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if upload_lease:
                    # Active uploads are skipped, never waited out by a sweep.
                    yield None
                    return
                if time.monotonic()>=deadline:
                    raise StorageError("Spool lock timed out") from None
                time.sleep(0.01)
        try:
            yield target
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(file,fcntl.LOCK_UN)


def _sync_dir(path):
    if os.name != "nt":
        fd = os.open(path,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic(path, content):
    temporary = path.parent / ("."+path.name+"."+uuid4().hex+".tmp")
    try:
        with temporary.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary,path)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_file(path, maximum):
    if path.is_symlink() or path.stat().st_size > maximum:
        raise StorageError("Unsafe or oversized spool file")
    with path.open("rb") as file:
        content = file.read(maximum+1)
    if len(content)>maximum:
        raise StorageError("Oversized spool file")
    return content


def _open(target):
    if not (target / "manifest.json").exists():
        return None
    settings = _get().settings
    manifest = json.loads(_read_file(target/"manifest.json",settings.max_markdown_bytes))
    if not isinstance(manifest,dict) or set(manifest)!={"source","images"}:
        raise StorageError("Invalid spool manifest")
    source = UploadSource.model_validate(manifest["source"])
    images = TypeAdapter(list[ImageRef]).validate_json(json.dumps(manifest["images"]))
    markdown = _read_file(target/"converted.md",settings.max_markdown_bytes).decode("utf-8")
    ConvertedDocument(markdown=markdown,images=images)
    return StagedUpload(path=target/"converted.md",source=source,images=images)


def _stage(revision_id,document,source):
    document = ConvertedDocument.model_validate_json(document.model_dump_json())
    source = UploadSource.model_validate_json(source.model_dump_json())
    content = document.markdown.encode("utf-8")
    if len(content)>_get().settings.max_markdown_bytes:
        raise StepError("invalid_upload",False)
    ref = f"uploads/{revision_id}"
    with _guard(ref) as target:
        existing = _open(target)
        if existing is not None:
            if (existing.source!=source or existing.images!=document.images or existing.path.read_bytes()!=content):
                raise StepError("staged_upload_conflict",False)
            return ref
        target.mkdir(exist_ok=True,mode=0o700)
        _atomic(target/"converted.md",content)
        manifest = json.dumps({"source":source.model_dump(mode="json"),
            "images":[image.model_dump(mode="json") for image in document.images]},ensure_ascii=False).encode("utf-8")
        _atomic(target/"manifest.json",manifest)
        _sync_dir(target.parent)
        return ref


def _open_staged(ref):
    with _guard(ref) as target:
        return _open(target)


def _remove(ref):
    with _guard(ref) as target:
        if target.exists():
            # _directory verifies the resolved absolute target is below uploads.
            shutil.rmtree(target)
            _sync_dir(target.parent)


def _candidates(older_than, limit, after=None):
    root = _get().settings.spool_dir.resolve()/"uploads"
    if not root.exists():
        return []
    if root.is_symlink():
        raise StorageError("Unsafe spool directory")
    candidates=[]
    for path in root.iterdir():
        try:
            _staging_revision("uploads/"+path.name)
        except ValueError:
            continue
        ref = 'uploads/' + path.name
        if (not path.is_symlink() and path.is_dir() and path.stat().st_mtime<older_than.timestamp()
                and (after is None or ref > after)):
            candidates.append(ref)
    return sorted(candidates)[:limit]


def _prepared(ref, value=None):
    from rag.runtime.upload_spool import PreparedRevision
    with _guard(ref) as target:
        path=target/"prepared.json"
        existing = (PreparedRevision.model_validate_json(_read_file(path,_get().settings.max_prepared_bytes))
                    if path.exists() else None)
        if value is None:
            if existing is not None:
                _check_prepared(ref,existing,_open(target))
            return existing
        value=PreparedRevision.model_validate_json(value.model_dump_json())
        _check_prepared(ref,value,_open(target))
        if existing is not None:
            if existing!=value:
                raise StepError("prepared_revision_conflict",False)
            return None
        content=value.model_dump_json().encode("utf-8")
        if len(content)>_get().settings.max_prepared_bytes:
            raise StorageError("Prepared revision exceeds configured limit")
        _atomic(path,content)


def _check_batch(batch, upload):
    if upload is None:
        raise StorageError("Accepted Markdown is missing")
    if batch.source != upload.source:
        raise StepError("document_batch_conflict", False)
    allowed = {image.image_id: image for image in upload.images}
    actual = {image.image_id: image for chunk in batch.chunks for image in chunk.images}
    if actual != allowed:
        raise StepError("document_batch_conflict", False)


def _batch(ref, value=None):
    with _guard(ref) as target:
        path = target / "chunks.json"
        existing = (DocumentBatch.model_validate_json(
            _read_file(path, _get().settings.max_prepared_bytes)) if path.exists() else None)
        upload = _open(target)
        if existing is not None:
            try:
                _check_batch(existing, upload)
            except StepError:
                raise StorageError("Stored document batch does not match its manifest") from None
        if value is None:
            return existing
        value = DocumentBatch.model_validate_json(value.model_dump_json())
        _check_batch(value, upload)
        if existing is not None:
            if existing != value:
                raise StepError("document_batch_conflict", False)
            return None
        content = value.model_dump_json().encode("utf-8")
        if len(content) > _get().settings.max_prepared_bytes:
            raise StorageError("Document batch exceeds configured limit")
        _atomic(path, content)


def _check_prepared(ref, prepared, upload):
    settings=_get().settings
    if upload is None:
        raise StorageError("Accepted Markdown is missing")
    if prepared.revision_id!=_staging_revision(ref):
        raise StepError("prepared_revision_conflict",False)
    if prepared.embedding_profile!=settings.embedding_profile or prepared.preparation_profile!=settings.preparation_profile:
        raise StepError("preparation_profile_mismatch",False)
    allowed={image.image_id:image for image in upload.images}
    for point in [*prepared.points,*prepared.topic_points]:
        if len(point.dense)!=settings.embedding_dimensions:
            raise StepError("embedding_profile_mismatch",False)
    for point in prepared.points:
        if (point.source_name!=upload.source.filename or point.source_url!=upload.source.source_url
                or point.access_level != upload.source.access_level):
            raise StepError("prepared_revision_conflict",False)
        if any(allowed.get(image.image_id)!=image for image in point.images):
            raise StepError("prepared_revision_conflict",False)
    batch_path = upload.path.parent / "chunks.json"
    if batch_path.exists():
        batch = DocumentBatch.model_validate_json(_read_file(batch_path, settings.max_prepared_bytes))
        _check_batch(batch, upload)
        if len(prepared.points) != len(batch.chunks):
            raise StepError("prepared_revision_conflict", False)
        for point, chunk in zip(prepared.points, batch.chunks, strict=True):
            if (point.access_level != batch.source.access_level or point.text != chunk.text
                    or point.locator != chunk.locator or point.images != chunk.images):
                raise StepError("prepared_revision_conflict", False)


def _call(function,*args):
    try:
        return function(*args)
    except (OSError,UnicodeError,ValidationError,json.JSONDecodeError) as exc:
        raise StorageError("Spool operation was not confirmed") from exc
