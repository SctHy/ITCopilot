"""Bounded database-backed attachments with task ACLs, digest and evidence events."""
import hashlib
import pathlib
from sqlalchemy import select, insert
from core.db import task_files
from workflow.tasks import TaskConflict, key

MAX_FILE_SIZE = 5 * 1024 * 1024
TYPES = {'.txt': 'text/plain', '.log': 'text/plain', '.csv': 'text/csv', '.json': 'application/json',
         '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.pdf': 'application/pdf'}


class TaskFiles:
    def __init__(self, tasks):
        self.tasks = tasks

    def upload(self, task_id, principal, name, content, client_id):
        name = pathlib.PurePosixPath(name.replace('\\', '/')).name
        if not name or len(name) > 200 or any(ord(c) < 32 for c in name) or pathlib.PurePosixPath(name).suffix.lower() not in TYPES:
            raise TaskConflict('支持 txt、log、csv、json、png、jpg 和 pdf，文件名需有效')
        if not content or len(content) > MAX_FILE_SIZE:
            raise TaskConflict('附件大小需为 1 字节至 5 MB')
        digest = hashlib.sha256(content).hexdigest()
        file_id = key(task_id, principal.subject, 'file', client_id)
        with self.tasks.transaction(task_id, principal) as (db, task):
            old = db.execute(select(task_files).where(task_files.c.id == file_id)).mappings().first()
            if old:
                if old['sha256'] != digest or old['name'] != name:
                    raise TaskConflict('同一上传请求不能使用不同文件')
                return self.public(old)
            if task['status'] != 'open':
                raise TaskConflict('请先重新打开事项')
            event = self.tasks.record(db, task, principal, 'attachment', '上传材料：' + name,
                {'file_id': file_id, 'sha256': digest, 'size': len(content)}, event_id=key('file-event', file_id))
            row = dict(id=file_id, task_id=task_id, uploader=principal.subject, name=name, content=content,
                       size=len(content), mime=TYPES[pathlib.PurePosixPath(name).suffix.lower()], sha256=digest,
                       event_id=event['id'], created_at=self.tasks.clock())
            db.execute(insert(task_files).values(**row))
            return self.public(row)

    @staticmethod
    def public(row):
        return {k: v for k, v in row.items() if k != 'content'}

    def list(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            columns = [c for c in task_files.c if c.name != 'content']
            return [dict(r) for r in db.execute(select(*columns).where(task_files.c.task_id == task_id).order_by(task_files.c.created_at)).mappings()]

    def download(self, task_id, file_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            row = db.execute(select(task_files).where(task_files.c.task_id == task_id, task_files.c.id == file_id)).mappings().first()
            if not row:
                raise TaskConflict('附件不存在')
            return dict(row)
